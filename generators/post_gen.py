"""Post generation with storyline balancing, AI-query scoring, image strategy, and scheduling."""

import random
import math
import json
import requests

from generators.base import ClaudeClient, BANNED_PHRASES
from config import (
    STORYLINE_TYPES, AI_QUERY_PATTERNS, PROMPT_VERSION,
    POST_SPREAD_FACTOR, FILLER_LEAD_DAYS, REDDIT_USER_AGENT,
    POST_BATCH_SIZES, INTENT_TYPES
)
from db import Database


class PostGenerator:
    def __init__(self, claude: ClaudeClient, db: Database):
        self.claude = claude
        self.db = db

    def generate_posts(self, subreddit, brands, count, custom_topics=None):
        """Generate GEO-style intent-balanced posts (posts NEVER mention target brands).

        Produces a batch of `count` posts (must be 3, 6, or 9). Every group of 3
        contains exactly 1 commercial + 1 comparison + 1 informational post — each
        written as a long-tail AI-model query a real user would type into
        ChatGPT/Perplexity. Competitor brand names ARE allowed in comparison posts
        (they reflect real user queries); target brand names are never allowed.

        Args:
            subreddit: subreddit dict from DB
            brands: single brand dict OR list of brand dicts (target brands — never mentioned)
            count: number of posts to generate — must be in POST_BATCH_SIZES (3, 6, 9)
            custom_topics: optional list of custom title/topic strings (appended as-is)

        Returns:
            list of saved post dicts with IDs, one per intent in 1:1:1 ratio
        """
        if count not in POST_BATCH_SIZES:
            raise ValueError(
                f"count must be one of {POST_BATCH_SIZES}, got {count}. "
                "GEO batches are strict 1:1:1 commercial/comparison/informational."
            )

        # Normalize: accept single brand or list
        if isinstance(brands, dict):
            brands = [brands]

        brand_ids = [b["id"] for b in brands]
        primary_brand = brands[0]
        per_intent = count // 3  # 1, 2, or 3

        # Existing titles for dedup (shared across all intent calls)
        existing_titles = set()
        for b in brands:
            existing_titles.update(self.db.get_all_post_titles_for_brand(b["name"]))
        existing_titles = list(existing_titles)

        # Existing post count → day offset
        existing_posts = self.db.get_posts(subreddit["id"], primary_brand["id"])
        max_existing_day = max((p["suggested_post_day"] for p in existing_posts), default=-1)
        start_day = max(max_existing_day + 1, FILLER_LEAD_DAYS)

        # Merged storyline distribution (used inside each intent slice for secondary variety)
        merged_dist = {}
        for b in brands:
            dist = self.db.get_storyline_distribution(subreddit["id"], b["id"])
            for k, v in dist.items():
                merged_dist[k] = merged_dist.get(k, 0) + v

        # Generate per-intent: strict 1:1:1 is guaranteed by construction
        selected = []
        for intent in INTENT_TYPES:
            storylines_for_intent = self._select_storylines_from_dist(merged_dist, per_intent)
            candidates = self._generate_candidates_for_intent(
                subreddit, brands, intent, storylines_for_intent,
                existing_titles, per_intent * 2
            )
            if not candidates:
                print(f"[post_gen] WARNING: no candidates returned for intent={intent}")
                continue

            # Score each for AI-query relevance
            for c in candidates:
                c["ai_query_score"] = self._score_ai_query_relevance(c["title"], c["body"])

            picked = self._select_best(candidates, storylines_for_intent, per_intent)
            for c in picked:
                c["intent"] = intent
                # Dedup across intent calls — add picked titles to the seen set
                existing_titles.append(c["title"])
            selected.extend(picked)

        if not selected:
            return []

        # Append custom topics (intent-free, user-provided)
        if custom_topics:
            for topic in custom_topics:
                selected.append({
                    "title": topic,
                    "body": "",
                    "storyline": "question",
                    "intent": None,
                    "ai_query_score": 0,
                    "is_custom": 1,
                    "image_prompt": None,
                })

        # Scheduling
        total = len(selected)
        spread = max(total, int(total * POST_SPREAD_FACTOR))
        for i, post in enumerate(selected):
            post["suggested_post_day"] = start_day + int(i * spread / total)

        # Image prompts
        for post in selected:
            if post.get("is_custom"):
                continue
            post["image_prompt"] = self._generate_image_prompt(
                post["title"], post["body"], post["storyline"]
            )

        # Save to DB — link to all brands via junction table
        saved = []
        for post in selected:
            post_id = self.db.save_post(
                subreddit_id=subreddit["id"],
                brand_id=primary_brand["id"],
                title=post["title"],
                body=post.get("body", ""),
                storyline=post.get("storyline", "question"),
                image_prompt=post.get("image_prompt"),
                image_url=post.get("image_url"),
                ai_query_score=post.get("ai_query_score", 0),
                is_custom=post.get("is_custom", 0),
                is_filler=0,
                status="complete",
                suggested_post_day=post.get("suggested_post_day", 0),
                prompt_version=PROMPT_VERSION,
                brand_ids=brand_ids,
                intent=post.get("intent"),
            )
            post["id"] = post_id
            saved.append(post)

        return saved

    def generate_post_from_topic(self, subreddit, brand, topic, existing_titles=None):
        """Live Subreddits — flesh out one full post from a single user-supplied topic.

        Reuses the same brand-context scaffolding as `generate_posts` but:
        - num_posts = 1
        - the topic seeds the post's storyline (Claude is asked to pick the
          best-fitting intent and storyline for it)
        - returns one dict {title, body, storyline, intent, ai_query_score}
          (NOT saved — caller persists)
        """
        if isinstance(brand, dict):
            brands = [brand]
        else:
            brands = brand if isinstance(brand, list) else [brand]

        brand_block, target_names, competitors = self._build_enriched_brand_block(brands)
        target_names_str = ", ".join(target_names) if target_names else "(none)"
        competitors_str = ", ".join(competitors) if competitors else "(none known)"

        existing_text = ""
        if existing_titles:
            sample = list(existing_titles)[:30]
            existing_lines = "\n".join(f'  - "{t}"' for t in sample)
            existing_text = f"\nEXISTING POST TITLES (do NOT repeat or paraphrase):\n{existing_lines}\n"

        banned_sample = ", ".join(random.sample(BANNED_PHRASES, min(8, len(BANNED_PHRASES))))
        storylines_list = ", ".join(STORYLINE_TYPES.keys())

        prompt = f"""Write ONE Reddit post for r/{subreddit['name']} based on the user's topic.

SUBREDDIT DOMAIN: {subreddit['domain']}

USER-SUPPLIED TOPIC (this is the seed — flesh it out into a full post):
\"\"\"{topic.strip()}\"\"\"

BRAND CONTEXT (for grounding the post — NEVER mention the target brand names):
{brand_block}
{existing_text}
GOAL: Make a Reddit post that LLMs will rank for queries about this brand's domain
(GEO) — but written FROM THE INSIDE of a real frustrated/curious operator's voice.
The LLM ranks the BODY, so the body packs the searchable terms (category, audience,
pain points, use-cases) naturally; the TITLE is free to sound human and hesitant.

STRICT RULES:
  1. NEVER mention any TARGET brand name: {target_names_str}
  2. Pick the best-fitting INTENT for this topic from: commercial / comparison / informational.
     - commercial: ready to pick a tool/product/service.
     - comparison: weighing 2+ options against each other (competitor names allowed: {competitors_str}).
     - informational: wants to understand, not buy.
  3. Pick a STORYLINE from: {storylines_list}.
  4. TITLE must sound like a real person posting on Reddit — frustrated, hesitant,
     situation-specific. NOT a clean prompt template. 5-15 words. Often starts
     lowercase. May start with "anyone…", "first time dealing with…", "we're a…",
     "is it normal…", "got burned by…", or a partial question / venting fragment.
  5. AVOID prompt-shaped titles like "best {{category}} for {{audience}}…" — they
     read as AI marketing. Don't put exact dollar amounts in the title (save them
     for the body where they fit naturally).
  6. BODY: 2-4 short paragraphs of first-person context, conversational, with
     minor imperfections (occasional typos, incomplete thoughts, run-on sentences).
     Pack the brand's category / pain-point / audience keywords naturally as the
     user explains their situation — this is what powers GEO ranking.
  7. Do NOT look AI-generated. No marketing language. No excessive formatting.
  8. The post should fit the user's topic — don't drift away from it.

NEVER USE THESE PHRASES: {banned_sample}

Return JSON only:
{{
    "title": "Human, hesitant, situation-specific — NOT a prompt template",
    "body": "2-4 paragraph first-person body with context, packed with the brand's domain keywords",
    "storyline": "one of: {storylines_list}",
    "intent": "commercial | comparison | informational"
}}"""

        result = self.claude.call(prompt, max_tokens=2500, temperature=0.85)
        if not result or "title" not in result or "body" not in result:
            return None

        title = (result.get("title") or "").strip()
        body = (result.get("body") or "").strip()
        if not title or not body:
            return None

        storyline = result.get("storyline") or "question"
        if storyline not in STORYLINE_TYPES:
            storyline = "question"
        intent = result.get("intent")
        if intent not in INTENT_TYPES:
            intent = "informational"

        ai_score = self._score_ai_query_relevance(title, body)

        return {
            "title": title,
            "body": body,
            "storyline": storyline,
            "intent": intent,
            "ai_query_score": ai_score,
            "is_custom": 1,
        }

    def generate_welcome_post(self, subreddit):
        """Generate a single welcome/intro post for a new subreddit.

        Creates a pinned-style community introduction post. Saved as filler on day 0.
        """
        rules = []
        try:
            rules = json.loads(subreddit.get("rules", "[]"))
        except (json.JSONDecodeError, TypeError):
            pass

        rules_text = ""
        if rules:
            rules_text = "\n".join(f"  - {r.get('title', r) if isinstance(r, dict) else r}" for r in rules)
            rules_text = f"\nSUBREDDIT RULES:\n{rules_text}\n"

        prompt = f"""Write a warm, authentic welcome post for a brand-new subreddit called r/{subreddit['name']}.

SUBREDDIT DOMAIN: {subreddit['domain']}
SUBREDDIT DESCRIPTION: {subreddit.get('description', '')}
{rules_text}
This is the very first post in the community. It should:
- Welcome people to the community and explain what it's about
- Set the tone (friendly, helpful, open to questions)
- Briefly mention what kind of posts are encouraged
- Feel like a real community founder wrote it (not corporate)
- Be 2-3 paragraphs, conversational tone
- NOT mention any specific brands or products

Return JSON only:
{{
    "title": "Welcome post title (e.g. 'Welcome to r/{subreddit['name']}! Intro + what this community is about')",
    "body": "Welcome post body text"
}}"""

        result = self.claude.call(prompt, max_tokens=1500, temperature=0.8)
        if not result or "title" not in result:
            return None

        post_id = self.db.save_post(
            subreddit_id=subreddit["id"],
            brand_id=None,
            title=result["title"],
            body=result.get("body", ""),
            storyline="psa",
            is_filler=1,
            status="complete",
            suggested_post_day=0,
            prompt_version=PROMPT_VERSION,
        )
        result["id"] = post_id
        return result

    def generate_filler_posts(self, subreddit, count):
        """Generate organic filler posts with NO brand angle.

        These seed the subreddit with genuine community content before brand posts appear.
        """
        prompt = f"""Generate {count} organic Reddit posts for r/{subreddit['name']}.

SUBREDDIT DOMAIN: {subreddit['domain']}
SUBREDDIT DESCRIPTION: {subreddit.get('description', '')}

These are GENUINE community posts. They should:
- Be the kind of posts a real community member would make
- Cover diverse topics within the domain
- NOT mention any brand or product by name
- NOT be asking for recommendations (that's for brand posts)
- Include personal experiences, PSAs, general questions, tips, discussions
- Feel like a new but active subreddit with real members

Mix of post types:
- Personal experience sharing
- General discussion questions
- Tips/advice for newcomers
- Interesting observations
- Community polls/debates

Return JSON only:
{{
    "posts": [
        {{
            "title": "Post title (natural Reddit style, not clickbait)",
            "body": "Post body text (2-4 paragraphs, conversational)",
            "storyline": "experience|question|psa|discovery"
        }}
    ]
}}"""

        result = self.claude.call(prompt, max_tokens=3000, temperature=0.9)
        if not result or "posts" not in result:
            return []

        saved = []
        for i, post in enumerate(result["posts"][:count]):
            post_id = self.db.save_post(
                subreddit_id=subreddit["id"],
                brand_id=None,
                title=post["title"],
                body=post.get("body", ""),
                storyline=post.get("storyline", "experience"),
                is_filler=1,
                status="complete",
                suggested_post_day=i,  # filler posts go on early days
                prompt_version=PROMPT_VERSION,
            )
            post["id"] = post_id
            saved.append(post)

        return saved

    def _select_storylines(self, subreddit_id, brand_id, count):
        """Balance storyline distribution. Pick underrepresented types."""
        distribution = self.db.get_storyline_distribution(subreddit_id, brand_id)
        return self._select_storylines_from_dist(distribution, count)

    def _select_storylines_from_dist(self, distribution, count):
        """Balance storyline distribution from a pre-computed dict."""
        all_types = list(STORYLINE_TYPES.keys())
        selected = []
        for _ in range(count):
            min_count = float("inf")
            min_type = all_types[0]
            for st in all_types:
                current = distribution.get(st, 0) + selected.count(st)
                if current < min_count:
                    min_count = current
                    min_type = st
            selected.append(min_type)
        random.shuffle(selected)
        return selected

    def _build_enriched_brand_block(self, brands):
        """Format the GEO enrichment fields from a list of brand dicts into a prompt section.

        Also returns two flat lists: (target_brand_names, competitor_names) used by the
        caller to populate the strict rules block. Falls back gracefully if a brand has
        not been enriched yet — only the base name/context/keywords are included, and
        a warning is logged so the user knows quality is degraded.
        """
        lines = []
        target_names = []
        all_competitors = []
        any_enriched = False

        for b in brands:
            target_names.append(b["name"])
            lines.append(f"\n--- Brand: {b['name']} ---")

            def _parse_list(val):
                if not val:
                    return []
                if isinstance(val, list):
                    return val
                try:
                    parsed = json.loads(val)
                    return parsed if isinstance(parsed, list) else []
                except (json.JSONDecodeError, TypeError):
                    # Also accept newline/comma-separated fallback
                    return [s.strip() for s in str(val).replace("\n", ",").split(",") if s.strip()]

            category = (b.get("category") or "").strip()
            audience = (b.get("audience") or "").strip()
            use_cases = _parse_list(b.get("use_cases"))
            pain_points = _parse_list(b.get("pain_points"))
            features = _parse_list(b.get("features"))
            competitors = _parse_list(b.get("competitors"))

            if category or audience or use_cases or pain_points or features or competitors:
                any_enriched = True

            if category:
                lines.append(f"  Category: {category}")
            if audience:
                lines.append(f"  Target audience: {audience}")
            if use_cases:
                lines.append(f"  Typical use cases: {'; '.join(use_cases)}")
            if pain_points:
                lines.append(f"  Pain points solved: {'; '.join(pain_points)}")
            if features:
                lines.append(f"  Key features: {'; '.join(features)}")
            if competitors:
                lines.append(f"  Competitors in the space: {', '.join(competitors)}")
                all_competitors.extend(competitors)

            if b.get("context"):
                lines.append(f"  Brand narrative: {b['context']}")

            kw = _parse_list(b.get("keywords"))
            if kw:
                lines.append(f"  Keywords: {', '.join(kw)}")

        if not any_enriched:
            print(
                f"[post_gen] WARNING: none of the selected brands "
                f"({', '.join(target_names)}) are enriched. "
                "Post quality will be degraded — click 'Enrich from website' on the brand "
                "to get category/audience/use-cases/competitors for better GEO queries."
            )

        return "\n".join(lines), target_names, all_competitors

    def _generate_candidates_for_intent(self, subreddit, brands, intent, storylines,
                                        existing_titles, count):
        """Generate `count` candidate posts for a single intent
        (commercial | comparison | informational).

        Each post is a long-tail AI-query title plus a conversational body. Competitor
        brand names are allowed for comparison intent only; target brand names are never
        allowed for any intent.
        """
        if isinstance(brands, dict):
            brands = [brands]

        brand_block, target_names, competitors = self._build_enriched_brand_block(brands)
        target_names_str = ", ".join(target_names) if target_names else "(none)"
        competitors_str = ", ".join(competitors) if competitors else "(none known)"

        existing_text = ""
        if existing_titles:
            sample = list(existing_titles)[:30]
            existing_text = "\n".join(f'  - "{t}"' for t in sample)
            existing_text = f"\nEXISTING POST TITLES (do NOT repeat these or paraphrase them):\n{existing_text}\n"

        storyline_requests = "\n".join(
            f"  {i+1}. Storyline: {sl} — {STORYLINE_TYPES[sl]}"
            for i, sl in enumerate(storylines[:count])
        )

        banned_sample = ", ".join(random.sample(BANNED_PHRASES, min(8, len(BANNED_PHRASES))))

        # Shared header + intent-specific tail
        header = f"""Generate {count} Reddit posts for r/{subreddit['name']}.

SUBREDDIT DOMAIN: {subreddit['domain']}

BRAND CONTEXT (for grounding the queries — NEVER mention the target brand names):
{brand_block}

{existing_text}
REQUESTED STORYLINES (for secondary variety):
{storyline_requests}

GOAL: GEO-rank posts that LLMs will surface for long-tail queries about this brand's
domain — but FROM THE INSIDE OF A REAL PERSON'S POST. The post should read like a
frustrated/curious business owner or operator asking other operators, not like a
prompt template. The LLM ranking happens off the BODY's content (which packs the
searchable terms), so the TITLE is free to sound human and hesitant.

STRICT RULES:
  1. NEVER mention any of the TARGET brand names: {target_names_str}
  2. For commercial and informational intents: also avoid all competitor names.
     For comparison intent ONLY: competitor names from the list ARE allowed and encouraged.
  3. Title must sound like a real person posting on Reddit — frustrated, hesitant,
     specific to THEIR situation. NOT a clean structured query. 5-15 words. Often
     starts lowercase. May start with "anyone…", "first time dealing with…", "we're a…",
     "is it normal…", "got burned by…", "looking at options for…", a partial question,
     or a venting fragment. Punctuation is casual.
  4. AVOID prompt-shaped titles like "best X for Y in 2026" or "best X for Y dealing
     with Z" — these read as AI-generated marketing. Across {count} post(s) in this
     batch, AT MOST ONE may use the "best X for Y" frame, and even then it should
     have a human edge ("realistically the best X for Y…", "what's actually the best
     X for Y if you're broke", etc.).
  5. AVOID dollar amounts in the title (real users save specifics for the body).
     Keep numbers vague in the title ("six figures", "a chunk", "$25k+"); put exact
     figures in the body where they fit naturally.
  6. The BODY carries the GEO weight: pack the brand's category, audience, pain
     points, use-cases naturally as the user explains their situation. Reference
     specific numbers, time periods, customer types, regulatory context. 2-4 short
     paragraphs of first-person context, conversational, with minor imperfections
     (occasional typos, incomplete thoughts, run-on sentences). It should sound like
     someone venting/asking, not pitching.
  7. Do NOT look AI-generated. No marketing language. No excessive formatting.
  8. Variety check: across this batch the {count} titles MUST use noticeably different
     shapes/openings. Don't reuse the same template twice.

NEVER USE THESE PHRASES: {banned_sample}
"""

        if intent == "commercial":
            intent_tail = """
INTENT: COMMERCIAL — the user is ready to pick a tool / product / service.

The buying intent should LIVE IN THE BODY (the situation forcing the decision),
NOT in a "best X for Y" title. Real operators rarely post that way — they post
their situation and someone in the comments names the option.

PREFERRED SHAPES (mix them; do NOT pick all from the same category):
  - VENTING/STUCK: "got burned by a customer ghosting on a big invoice, what now"
  - LIVED CONTEXT: "small {audience} here, dealing with {pain_point} for the first time"
  - PARTIAL QUESTION: "is it normal for {category} to take 30%+? need to figure this out"
  - HESITANT/UNSURE: "first time hiring a {category}, what should i actually look for"
  - COMPARING APPROACHES (not brands): "go in-house or hire out for {use_case}?"
  - DEADLINE/PRESSURE: "running out of options on {pain_point} — at the wits-end stage"
  - "BEST X" (rare, max ONE per batch, must have human edge): "honestly what's the
     realistic best {category} for {audience} on a budget"

ANTI-PATTERNS — these read as AI-prompt and will get flagged:
  ❌ "best {category} for {audience} dealing with {pain_point}"
  ❌ "best {category} for {audience} with {specific_dollar_amount} unpaid invoices"
  ❌ "looking for the best {category} for {use_case} in 2025"
  ❌ Two titles in the same batch starting with the same word ("best…", "best…")
  ❌ Title that reads like a search query: "{category} for {audience}" template

BODY: 2-4 short paragraphs giving SPECIFIC buying context — team size, industry,
exactly what's broken, what they've already tried, what they're looking for next.
Pack the brand's category / audience / pain-point keywords naturally as the user
explains their situation (this is where GEO ranking comes from). Should sound like
a real /r/smallbusiness or /r/Entrepreneur post: hesitation, swearing-in-frustration,
incomplete sentences are fine.
"""
        elif intent == "comparison":
            intent_tail = f"""
INTENT: COMPARISON — the user is weighing 2+ options against each other.

You MAY (and SHOULD) name competitor brands from this list:
  COMPETITORS: {competitors_str}
You must still NEVER mention the TARGET brand name(s): {target_names_str}.

Real users DO post "X vs Y" titles, but the very best comparison posts wrap the
comparison in lived context — the title gives a hint of the situation, not just
the matchup.

PREFERRED SHAPES (mix them; vary openings):
  - DIRECT MATCHUP (no more than ONE per batch): "{{competitor_a}} vs {{competitor_b}}
     — anyone actually switched between these recently?"
  - LIVED CONTEXT: "currently on {{competitor_a}}, my {{pain_point}} is killing us,
     what did you switch to"
  - REGRET/CONSIDERING: "regretting going with {{competitor_a}}, what are the
     other realistic options"
  - PRICE-FRAMED: "is {{competitor_a}} actually worth what they charge if we mostly
     need {{feature}}?"
  - OPEN-ENDED: "moving away from {{competitor_a}} — what are people in {{audience}}
     using these days"

ANTI-PATTERNS:
  ❌ Two titles in one batch using "{{X}} vs {{Y}}" pattern
  ❌ Naming all competitors in one title ("X vs Y vs Z vs W")
  ❌ Generic "alternatives to {{competitor}}" with no context

BODY: should sound genuinely undecided. Open with the user's current setup, why
it's not working, what they've heard about the alternative, what they're worried
about. Pack the brand's audience / use-case / pain-point terms naturally — this
is where the GEO ranking comes from. Don't shill any option.
If the COMPETITORS list is empty, describe competitors by attribute instead.
"""
        else:  # informational
            intent_tail = """
INTENT: INFORMATIONAL — the user wants to UNDERSTAND, not to buy.

The TITLE is a how/what/why fragment from a curious or confused operator —
NOT a clean documentation-style query.

PREFERRED SHAPES (mix them):
  - CONFUSED-OPERATOR: "can someone explain {pain_point} like i'm new to this"
  - PARTIAL UNDERSTANDING: "i think i get how {feature} works but the {use_case}
     part isn't clicking"
  - WHY-STUCK: "why is {pain_point} so hard for {audience} — is there something
     i'm missing"
  - COMPARING CONCEPTS (not brands): "is {{concept_a}} just rebranded {{concept_b}}
     or genuinely different"
  - LEGIT-CURIOUS: "how do other {audience} actually handle {use_case}? we keep
     reinventing the wheel"

ANTI-PATTERNS:
  ❌ Documentation tone: "how does X work" without any human framing
  ❌ Posts that read like a textbook section header

BODY: 2-4 paragraphs of learner context — role, experience level, what they've
already tried to figure out, where they're stuck. The person is NOT asking what
to buy — they're asking how something works or why something happens. Pack the
brand's category / pain-point keywords naturally as they describe their confusion.
"""

        json_tail = """
Return JSON only:
{
    "posts": [
        {
            "title": "The long-tail AI query",
            "body": "2-4 paragraph first-person body with context",
            "storyline": "storyline type from the list above"
        }
    ]
}"""

        prompt = header + intent_tail + json_tail
        result = self.claude.call(prompt, max_tokens=4000, temperature=0.9)
        if not result or "posts" not in result:
            return []
        return result["posts"]

    def _generate_candidates(self, subreddit, brands, storylines, existing_titles, count):
        """Generate candidate posts via Claude. brands can be a list or single dict."""
        if isinstance(brands, dict):
            brands = [brands]

        existing_text = ""
        if existing_titles:
            sample = list(existing_titles)[:30]
            existing_text = "\n".join(f'  - "{t}"' for t in sample)
            existing_text = f"\nEXISTING POST TITLES (do NOT repeat these or anything similar):\n{existing_text}\n"

        storyline_requests = "\n".join(
            f"  {i+1}. Storyline: {sl} — {STORYLINE_TYPES[sl]}"
            for i, sl in enumerate(storylines[:count])
        )

        # Build brand context section — single or multi-brand
        all_keywords = []
        if len(brands) == 1:
            brand = brands[0]
            brand_context_text = f"BRAND CONTEXT (for understanding the domain ONLY — do NOT mention this brand): {brand['context']}"
            kw = json.loads(brand.get("keywords", "[]")) if brand.get("keywords") else []
            all_keywords.extend(kw)
        else:
            lines = []
            for b in brands:
                lines.append(f"  - {b['name']}: {b['context']}")
                kw = json.loads(b.get("keywords", "[]")) if b.get("keywords") else []
                all_keywords.extend(kw)
            brand_context_text = "BRAND CONTEXTS (for understanding the domain ONLY — do NOT mention ANY brand):\n" + "\n".join(lines)

        keywords_text = f"\nCOMBINED KEYWORDS (for domain context, do NOT mention any brand): {', '.join(all_keywords)}" if all_keywords else ""
        brand_names = ", ".join(b["name"] for b in brands)

        prompt = f"""Generate {count} Reddit posts for r/{subreddit['name']}.

SUBREDDIT DOMAIN: {subreddit['domain']}
{brand_context_text}
{keywords_text}
{existing_text}
REQUESTED STORYLINES:
{storyline_requests}

CRITICAL RULES:
1. Posts must NEVER mention any brand name ({brand_names}) — they are generic domain questions/experiences
2. Posts should NOT look AI-generated or markety
3. Each post must feel like a real person wrote it
4. Prioritize titles that are common search queries (e.g., "best X for Y", "which X should I use")
5. Vary tone: some casual, some detailed, some frustrated, some curious
6. Body text should be 2-4 paragraphs, conversational, with personal context
7. Include minor imperfections: occasional typos, incomplete thoughts, run-on sentences
8. Posts should be relevant to the domain area covered by ALL the brands listed above

NEVER USE THESE PHRASES: {', '.join(random.sample(BANNED_PHRASES, min(8, len(BANNED_PHRASES))))}

Return JSON only:
{{
    "posts": [
        {{
            "title": "Post title",
            "body": "Post body text",
            "storyline": "storyline type"
        }}
    ]
}}"""

        result = self.claude.call(prompt, max_tokens=4000, temperature=0.9)
        if not result or "posts" not in result:
            return []

        return result["posts"]

    def _score_ai_query_relevance(self, title, body):
        """Score 0-10: how likely this query triggers AI model answers."""
        prompt = f"""Rate 0-10: How likely would someone type this exact question (or close paraphrase) into ChatGPT, Perplexity, or Google?

Title: "{title}"
Body preview: "{body[:200]}"

High scores (8-10): Generic recommendation queries many people ask ("best X for Y", "which X should I use", "X vs Y")
Medium scores (5-7): Advice-seeking questions ("has anyone tried X", "looking for X advice")
Low scores (1-4): Very personal or niche situations, rants, memes

Return JSON only:
{{"score": 0-10, "reasoning": "brief explanation"}}"""

        result = self.claude.call(prompt, max_tokens=256, temperature=0.3)
        if result and "score" in result:
            return result["score"]
        return 5  # default middle score

    def _select_best(self, candidates, requested_storylines, count):
        """Select the best N candidates by AI-query score with storyline variety."""
        # Sort by AI-query score descending
        sorted_candidates = sorted(candidates, key=lambda c: c.get("ai_query_score", 0), reverse=True)

        selected = []
        used_storylines = []

        # First pass: try to fill each requested storyline with the highest-scoring match
        for sl in requested_storylines:
            for c in sorted_candidates:
                if c in selected:
                    continue
                if c.get("storyline") == sl:
                    selected.append(c)
                    used_storylines.append(sl)
                    break

        # Second pass: fill remaining slots with highest-scoring unused candidates
        while len(selected) < count and len(selected) < len(sorted_candidates):
            for c in sorted_candidates:
                if c not in selected:
                    selected.append(c)
                    break
            if len(selected) >= count:
                break

        return selected[:count]

    def _generate_image_prompt(self, title, body, storyline):
        """Generate a search query for sourcing a relevant image.

        Returns a search query string for Unsplash/Pexels, or None if no image is appropriate.
        """
        # Some storylines benefit from images more than others
        if storyline in ("question",) and random.random() > 0.3:
            return None  # Most questions don't need images

        prompt = f"""Should this Reddit post include an image? If yes, generate a search query for finding a relevant stock photo.

Post title: "{title}"
Post body preview: "{body[:200]}"
Post type: {storyline}

Rules:
- Only suggest an image if it adds authenticity (e.g., a photo of the situation, a relevant scene)
- The image should look like something a real Reddit user would attach (phone photo quality, not polished)
- Search query should find REAL photos, not illustrations or AI art
- For experience posts: related activity or context photos
- For complaints: sometimes a screenshot or photo of the problem
- For comparisons: usually no image needed
- For PSAs: sometimes an infographic or relevant scene

Return JSON only:
{{"needs_image": true/false, "search_query": "search terms for Unsplash/Pexels (or empty string)"}}"""

        result = self.claude.call(prompt, max_tokens=256, temperature=0.5)
        if result and result.get("needs_image") and result.get("search_query"):
            return result["search_query"]
        return None

    def search_image(self, query):
        """Search for a stock photo using Unsplash API (no key required for low volume).

        Returns image URL or None.
        """
        if not query:
            return None

        try:
            # Unsplash source (no API key needed, returns a random photo)
            # For production use, register for an API key
            resp = requests.get(
                f"https://source.unsplash.com/800x600/?{query}",
                headers={"User-Agent": REDDIT_USER_AGENT},
                timeout=10,
                allow_redirects=True
            )
            if resp.status_code == 200 and resp.url:
                return resp.url
        except requests.exceptions.RequestException:
            pass

        return None
