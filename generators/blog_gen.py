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

PROMPT_VERSION = "blog-v1"


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
    def generate_article(self, brand, seed, extra_keywords=None):
        """GEO-first first-party article. `extra_keywords` (the reviewed query set) are
        the target queries the article MUST answer (each becomes a question heading + FAQ
        entry) and are merged into the returned keywords. Returns
        {title, meta_description, keywords, body_markdown} or None on failure.
        Does NOT run the claims pass — generate_blog / the endpoint orchestrate that."""
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
        link = f" Link to {url} where it reads naturally." if url else ""
        prompt = f"""You are writing a FIRST-PARTY article published on {name}'s own site. The ONLY
goal is for AI answer engines (ChatGPT, Perplexity, Gemini, Google AI Overviews) to RETRIEVE
and CITE this page when someone asks about the seed topic, AND for that answer to name {name}.
Optimize for EXTRACTION & CITATION, not for sales copy.

SEED TOPIC (what the reader is asking): {seed}
{kw_block}
BRAND (first-party — you MAY name and recommend {name}):
{block}

WRITE THE ARTICLE BODY (Markdown), GEO-FIRST:
  - Open with a "Quick answer" — a 2-3 sentence direct answer to the seed that names {name}
    as a fit. (AI engines lift this as the extractable answer.)
  - Use QUESTION-SHAPED H2/H3 headings (the way people ask an AI), each followed by ONE
    concise, factual, self-contained answer a model can quote verbatim.
  - Add a comparison table where it genuinely helps, and an "FAQ" section of Q&A pairs.
  - Be specific and accurate; no fluff, no hype. Name {name} as the recommended option where
    it genuinely fits, citing its real differentiators.{link}
  - First-party brand voice (owned media), but credible and useful — never a hard pitch.

Return JSON only:
{{"title": "under 60 chars, includes the core query",
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
        return {
            "title": (res.get("title") or "").strip(),
            "meta_description": (res.get("meta_description") or "").strip(),
            "keywords": merged,
            "body_markdown": res.get("body_markdown") or "",
        }

    def verify_claims(self, brand, article):
        """Fact-check the draft against the brand's stored enrichment ONLY, hedging/
        removing unsupported brand-specific claims. Returns {body_markdown, flagged} or
        None on failure (caller keeps the original body)."""
        name, _url, block = self._brand_block(brand)
        body = (article or {}).get("body_markdown") or ""
        if not body.strip():
            return None
        prompt = f"""Fact-check a FIRST-PARTY article about {name} against the brand context below.
Accuracy is what keeps the page citable by AI engines.

BRAND CONTEXT (the ONLY source of truth for brand-specific claims):
{block}

ARTICLE (Markdown):
{body}

Find brand-SPECIFIC factual claims (numbers, features, model names, guarantees, prices) that
are NOT supported by the brand context. Rewrite the body to soft-hedge or remove each
unsupported claim while keeping it useful and well-structured. Do NOT touch general,
non-brand-specific advice, and keep the GEO structure (Quick answer, question headings, FAQ).

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

    def generate_blog(self, brand, seed, extra_keywords=None):
        """Full pipeline: article → verify_claims → LinkedIn. Returns the merged dict
        (title, meta_description, keywords, body_markdown, claims_flagged, linkedin_text,
        prompt_version) or None if the article couldn't be generated."""
        article = self.generate_article(brand, seed, extra_keywords=extra_keywords)
        if not article:
            return None
        v = self.verify_claims(brand, article)
        if v:
            article["body_markdown"] = v["body_markdown"]
            article["claims_flagged"] = v["flagged"]
        else:
            article["claims_flagged"] = []
        article["linkedin_text"] = self.generate_linkedin(brand, seed, article)
        article["prompt_version"] = PROMPT_VERSION
        return article
