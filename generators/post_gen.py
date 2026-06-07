"""Post generation with storyline balancing, AI-query scoring, image strategy, and scheduling."""

import random
import math
import json
import requests

from generators.base import ClaudeClient, BANNED_PHRASES
from generators.comment_gen import classify_post_intent
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

    def generate_posts(self, subreddit, brands, count=None, custom_topics=None,
                       intent_counts=None, context_only=False, seed=None,
                       ai_search=False):
        """Generate GEO-style posts (posts NEVER mention target brands).

        `ai_search` (optional, default False): the new AI-Search semantic-coverage
        MODE. When True, runs ONE query fan-out pass (`_fanout_rewrites`) to derive
        the union rewrite cluster + a concept/phrasing checklist for the brand
        (optionally anchored on `seed`), then steers title/body generation to
        collectively cover that cluster. Posts are tagged
        `prompt_version=<v>-ai-search` and persist the checklist so the later HQ
        anchor can cover the whole cluster. ai_search=False is the unchanged
        standard path.

        `seed` (optional): an existing prompt/question, several of them, or a
        keyword/platform (e.g. "Instagram"). When set, every generated title is
        an expansion AROUND that seed's theme/platform — sibling recommendation
        questions that strengthen coverage of it — while still obeying all the
        normal title/brand rules. Threaded into `_generate_candidates_for_intent`
        as `seed_focus`.

        Two ways to size the batch:
          - `intent_counts`: an explicit per-intent map, e.g.
            {"commercial": 2, "comparison": 0, "informational": 3}. Generates
            exactly that many of each intent (the flexible mode used by the
            Live Subreddits generator). Intents with 0 are skipped.
          - `count`: legacy strict 1:1:1 batch — must be in POST_BATCH_SIZES
            (3, 6, 9). Each group of 3 = 1 commercial + 1 comparison + 1
            informational.

        Each post is written as a long-tail AI-model query a real user would
        type into ChatGPT/Perplexity. Competitor brand names ARE allowed in
        comparison posts; target brand names are never allowed.

        Args:
            subreddit: subreddit dict from DB. With `context_only=True` this is
                the SAVE TARGET only (e.g. the "unassigned" pool) — its
                name/domain are NOT injected into the prompt; posts are grounded
                purely in brand context so a real subreddit can be assigned
                later.
            brands: single brand dict OR list of brand dicts (never mentioned)
            count: legacy batch size (POST_BATCH_SIZES) — ignored if
                `intent_counts` is given
            custom_topics: optional list of custom title/topic strings
            intent_counts: optional {intent: n} map for flexible per-intent sizing
            context_only: when True, generate from brand context without scoping
                the prompt to a specific subreddit, and instruct the batch to
                collectively cover ALL the brand's offerings.

        Returns:
            list of saved post dicts with IDs
        """
        # Build the per-intent generation plan: [(intent, n), ...].
        if intent_counts:
            plan = []
            for it in INTENT_TYPES:
                try:
                    n = int(intent_counts.get(it, 0))
                except (TypeError, ValueError):
                    n = 0
                if n > 0:
                    plan.append((it, n))
            if not plan:
                raise ValueError("intent_counts must request at least one post")
        else:
            if count not in POST_BATCH_SIZES:
                raise ValueError(
                    f"count must be one of {POST_BATCH_SIZES}, got {count}. "
                    "GEO batches are strict 1:1:1 commercial/comparison/informational."
                )
            per_intent = count // 3  # 1, 2, or 3
            plan = [(it, per_intent) for it in INTENT_TYPES]

        # Normalize: accept single brand or list
        if isinstance(brands, dict):
            brands = [brands]

        brand_ids = [b["id"] for b in brands]
        primary_brand = brands[0]

        # AI-Search mode: one fan-out pass, reused across every intent slice.
        coverage_focus = None
        checklist_json = None
        if ai_search:
            coverage_focus = self._fanout_rewrites(brands, seed)
            if coverage_focus and coverage_focus.get("checklist"):
                checklist_json = json.dumps(coverage_focus["checklist"])

        # Existing titles for dedup (shared across all intent calls).
        # Scoped to THIS subreddit only — we intentionally allow the
        # same title to be reused in a different subreddit, since
        # cross-sub reposts are a valid strategy. Within the same sub
        # we still block duplicates (Reddit rejects exact-title reposts
        # in many subs).
        existing_titles = set()
        for b in brands:
            existing_titles.update(
                self.db.get_post_titles_for_brand_in_subreddit(
                    b["name"], subreddit["id"]
                )
            )
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

        # Generate per-intent per the plan built above.
        selected = []
        for intent, n_intent in plan:
            storylines_for_intent = self._select_storylines_from_dist(merged_dist, n_intent)
            candidates = self._generate_candidates_for_intent(
                subreddit, brands, intent, storylines_for_intent,
                existing_titles, n_intent * 2, context_only=context_only,
                # In AI-Search mode the fan-out already consumed the seed, so
                # we steer via coverage_focus instead of the seed block.
                seed_focus=(None if ai_search else seed),
                coverage_focus=coverage_focus,
            )
            if not candidates:
                print(f"[post_gen] WARNING: no candidates returned for intent={intent}")
                continue

            # Score each for AI-query relevance
            for c in candidates:
                c["ai_query_score"] = self._score_ai_query_relevance(c["title"], c["body"])

            picked = self._select_best(candidates, storylines_for_intent, n_intent)
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
                prompt_version=(PROMPT_VERSION + "-ai-search") if ai_search else PROMPT_VERSION,
                brand_ids=brand_ids,
                intent=post.get("intent"),
                concept_checklist=checklist_json,
            )
            post["id"] = post_id
            saved.append(post)

        return saved

    def generate_post_from_topic(self, subreddit, brand, topic, existing_titles=None):
        """Live Subreddits — flesh out one full post from a user-supplied title.

        The user-supplied `topic` is the FINAL post title, used verbatim. The
        LLM only writes the body, storyline, and intent that fit that title.
        It does NOT rewrite or "improve" the title.

        Reuses the same brand-context scaffolding as `generate_posts` but:
        - num_posts = 1
        - title is the user's input, unchanged
        - returns one dict {title, body, storyline, intent, ai_query_score}
          (NOT saved — caller persists)
        """
        if isinstance(brand, dict):
            brands = [brand]
        else:
            brands = brand if isinstance(brand, list) else [brand]

        # The user's input IS the final title. Don't let the model touch it.
        final_title = (topic or "").strip()
        if not final_title:
            return None

        # Classify the user-supplied title so the BODY can adapt:
        # short answer-asking titles get a short focused body that gives
        # just-enough context for someone to recommend something; longer
        # experience-style titles get a richer narrative body. Without
        # this, every custom post got the same "2-4 paragraphs" body
        # regardless of whether the title was a 6-word question or a
        # 30-word personal-story setup.
        intent_info = classify_post_intent(final_title, post_body=None, stored_intent=None)
        intent_label = intent_info["intent_label"]
        if intent_info["target_length_band"] == "crisp":
            body_target = "1-2 short paragraphs (max ~100 words). Just enough context for someone to give a useful recommendation — your situation, what you've considered, what's pushing you to ask."
        elif intent_info["target_length_band"] == "long":
            body_target = "3-5 paragraphs (~150-280 words). Richer first-person narrative — context, what you've tried, where you are now, what's still bugging you."
        else:
            body_target = "2-3 short paragraphs (~100-180 words). First-person context with some detail — situation, options you've weighed, what you're stuck on."

        brand_block, target_names, competitors = self._build_enriched_brand_block(brands)
        target_names_str = ", ".join(target_names) if target_names else "(none)"
        competitors_str = ", ".join(competitors) if competitors else "(none known)"

        existing_text = ""
        if existing_titles:
            sample = list(existing_titles)[:30]
            existing_lines = "\n".join(f'  - "{t}"' for t in sample)
            existing_text = f"\nEXISTING POST TITLES on this brand (avoid drifting into duplicate-feeling territory):\n{existing_lines}\n"

        banned_sample = ", ".join(random.sample(BANNED_PHRASES, min(8, len(BANNED_PHRASES))))
        storylines_list = ", ".join(STORYLINE_TYPES.keys())

        prompt = f"""Write the BODY of a Reddit post for r/{subreddit['name']}.

The TITLE is FIXED — it is the user's exact text. Do NOT rewrite,
shorten, expand, paraphrase, or improve the title in any way. Your job
is ONLY to write a body, storyline, and intent that fit this title.

POST TITLE (FIXED — use exactly as given, do not modify):
\"\"\"{final_title}\"\"\"

SUBREDDIT DOMAIN: {subreddit['domain']}

BRAND CONTEXT (for grounding the body — NEVER mention the target brand names):
{brand_block}
{existing_text}
GOAL: write a body that fits the title above and is also retrievable by
AI search engines (GEO) for queries about this brand's domain. The LLM
ranks the BODY, so pack the brand's category / audience / pain-point /
use-case keywords naturally as the user explains their situation.

STRICT RULES:
  1. NEVER mention any TARGET brand name: {target_names_str}
  2. Pick the best-fitting INTENT for this title from:
     commercial / comparison / informational.
     - commercial: ready to pick a tool/product/service.
     - comparison: weighing 2+ options (competitor names allowed: {competitors_str}).
     - informational: wants to understand, not buy.
  3. Pick a STORYLINE from: {storylines_list} that fits the title.
  4. BODY: {body_target}
     Conversational first-person tone with minor imperfections
     (occasional typos, incomplete thoughts, run-on sentences). Pack the
     brand's category / pain-point / audience keywords naturally as the
     user explains their situation — this is what powers GEO ranking.
     The detected post intent for this title is: {intent_label}.
     The body MUST include a natural-language phrasing of the underlying
     long-tail query somewhere inside it (e.g. "trying to figure out the
     best [category] for [audience] dealing with [pain-point]") — woven
     into a sentence the user might naturally write, NOT as a header.
  5. The body must be COMPATIBLE with the title — answer / expand / give
     context for whatever question or situation the title raises. Do NOT
     drift to a different topic.
  6. Do NOT look AI-generated. No marketing language. No excessive
     formatting. No emoji. No dashes (-, —, --).
  7. Do NOT include the title text inside the body verbatim.

NEVER USE THESE PHRASES: {banned_sample}

Return JSON only (note: NO "title" field — the title is fixed and we
will use the user's input verbatim):
{{
    "body": "2-4 paragraph first-person body with context, packed with the brand's domain keywords",
    "storyline": "one of: {storylines_list}",
    "intent": "commercial | comparison | informational"
}}"""

        # Capture the LLM result + log diagnostics on every failure
        # mode so 'Topic generation failed' on the UI side becomes
        # debuggable from stdout instead of an opaque None.
        try:
            result = self.claude.call(prompt, max_tokens=2500, temperature=0.85)
        except Exception as e:
            print(f"[generate_post_from_topic] LLM exception: {type(e).__name__}: {e}", flush=True)
            return None
        if result is None:
            print(f"[generate_post_from_topic] LLM returned None — see ClaudeClient.call retry logs above. title={final_title!r}", flush=True)
            return None
        if "body" not in result:
            print(f"[generate_post_from_topic] LLM response missing 'body' key. Keys: {list(result.keys())}. title={final_title!r}", flush=True)
            return None
        body = (result.get("body") or "").strip()
        if not body:
            print(f"[generate_post_from_topic] LLM returned empty body. title={final_title!r}", flush=True)
            return None

        storyline = result.get("storyline") or "question"
        if storyline not in STORYLINE_TYPES:
            storyline = "question"
        intent = result.get("intent")
        if intent not in INTENT_TYPES:
            intent = "informational"

        ai_score = self._score_ai_query_relevance(final_title, body)

        return {
            "title": final_title,  # user input, verbatim — never touched by the LLM
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

    def _fanout_rewrites(self, brands, seed=None):
        """AI-Search mode: one Claude call that simulates how ChatGPT / Perplexity /
        Gemini fan a prompt out into sub-queries, then UNIONs them into a master
        rewrite cluster + a concept/phrasing checklist for the brand's space.

        Returns {"rewrites": [str, ...], "checklist": [str, ...]} or None on failure
        (callers treat None as "no coverage steering" and fall back to normal gen).
        The flavoring (per-engine styles) happens INSIDE the single call; the output
        is engine-agnostic because the deliverable (one Reddit thread cluster) is
        shared across engines and retrieval is semantic.
        """
        if isinstance(brands, dict):
            brands = [brands]
        brand_block, target_names, _competitors = self._build_enriched_brand_block(brands)
        seed_line = ""
        if seed and str(seed).strip():
            seed_line = (
                "\nANCHOR SEED (expand the fan-out AROUND this — an existing "
                f"prompt/question, several, or a keyword/platform):\n{str(seed).strip()}\n"
            )
        prompt = f"""You are mapping the AI-search query space for a GEO campaign. The goal is to get
this brand recommended by AI assistants. AI engines REWRITE a user's prompt into
several search sub-queries ("query fan-out") and retrieve SEMANTICALLY, so we need
the full cluster of phrasings around the brand's buying/recommendation intent —
NOT one literal phrasing.

BRAND CONTEXT (ground the queries here; NEVER output the target brand name(s): {', '.join(target_names) or '(none)'}):
{brand_block}
{seed_line}
Do this in your head, then return the merged result:
  1. Imagine how each engine fans the intent out:
     - ChatGPT: a FEW (2-3) natural-language queries close to how a person phrases it.
     - Perplexity: SEVERAL (4-6) short, keyword-style queries.
     - Gemini / AI Overviews: a WIDE set of decomposed sub-questions and angles.
  2. UNION + DEDUPE them into one master list of distinct rewrites (paraphrases,
     decomposed sub-questions, and modifier/entity variants) that a real person
     could plausibly ask and whose natural answer is to RECOMMEND a product/service
     in this brand's niche. Cover distinct REGIONS of the space (different
     use-cases / platforms / buyer concerns), not just synonyms of one query.
  3. Produce a concise CONCEPT/PHRASING CHECKLIST: the key natural-language
     phrasings, synonyms and domain terms a Reddit thread should contain so it
     matches the whole cluster for both keyword (BM25) and embedding retrieval.

Return JSON only:
{{
  "rewrites": ["distinct sub-query 1", "distinct sub-query 2", "..."],
  "checklist": ["phrasing/term 1", "phrasing/term 2", "..."]
}}
Aim for 12-20 rewrites and 8-15 checklist items. Never include the target brand name."""
        result = self.claude.call(prompt, max_tokens=1500, temperature=0.7)
        if not result or not isinstance(result, dict):
            return None
        rewrites = result.get("rewrites") or []
        checklist = result.get("checklist") or []
        if not rewrites and not checklist:
            return None
        return {"rewrites": rewrites, "checklist": checklist}

    def _generate_candidates_for_intent(self, subreddit, brands, intent, storylines,
                                        existing_titles, count, context_only=False,
                                        seed_focus=None, coverage_focus=None):
        """Generate `count` candidate posts for a single intent
        (commercial | comparison | informational).

        Each post is a long-tail AI-query title plus a conversational body. Competitor
        brand names are allowed for comparison intent only; target brand names are never
        allowed for any intent.

        `coverage_focus` (AI-Search mode): {"rewrites": [...], "checklist": [...]}
        from the fan-out pass. When set, the batch's titles must collectively span
        DISTINCT rewrites in the cluster and each body must semantically cover the
        checklist phrasings (so the thread is retrievable for the whole query
        cluster, not just one literal phrasing).
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

        # Universal title strategy (ALL intents). The TITLE must be a
        # recommendation-eliciting question — one whose natural AI answer is to
        # name a specific product/service — and narrowed to the brand's niche so
        # THIS brand is the natural pick. Expressed as instructions only: NO
        # example titles and NO fixed pattern (either would become a template the
        # model defaults to, flattening variety and anchoring the domain).
        title_rules = """  3. TITLE = THE RECOMMENDATION QUESTION SOMEONE ASKS AN AI. Write the title as a
     real, natural question a person types into ChatGPT / Perplexity / Google when
     they want a recommendation for what to USE or BUY. The point: when an AI is
     asked this, the helpful answer is to NAME a specific product / service /
     provider — and, given the BRAND CONTEXT above, this brand is one that fits.
     Every title must pass BOTH checks:
       • RECOMMENDATION CHECK — if a helpful AI answered this title, would it
         recommend a specific product/service to use? If it would instead just
         explain a concept, give general tips, or describe "what to look for,"
         the title is wrong — rewrite it as a request for a recommendation.
       • OUR-BRAND CHECK — would THIS brand be a natural answer? Read the BRAND
         CONTEXT to decide which kind of brand it is:
           - SPECIFIC product or service → narrow the question by its category /
             who it's for / the problem or use-case it's best at, so it's a
             natural pick and not a query only a big generic market leader wins.
           - SUPPLIER / RETAILER / MARKETPLACE / DIRECTORY that sells or
             aggregates many products or providers → category-level BUY-INTENT
             questions are on-target: asking where to buy / the best place to
             order / who sells the category (online, with fast shipping, with
             financing, for pros, hard-to-find gear, etc.). The brand IS the
             literal answer to those, so do NOT force it down to a single
             product — across the batch, MIX these supplier-level questions with
             more specific product/task ones.
         Either way, NEVER write the target brand name in the title, and never
         turn it into a generic info / "do they work" / how-it-works question.
  4. Keep titles short (~3-12 words) and human — a real person could post it. VARY
     the wording and structure across the batch; do NOT fall into one repeated
     shape, do NOT keyword-stuff, do NOT append years ("2025"). Mix more
     conversational phrasings with more direct search-style ones, but EVERY title
     must still lead to recommending a specific product/service."""

        # Subreddit scoping line. In context_only mode (no subreddit chosen
        # yet) we deliberately DON'T name a subreddit — the post is grounded
        # purely in brand context so it can be placed in whichever subreddit
        # fits best when it's assigned later. We also tell the model to make
        # the batch collectively cover ALL of the brand's offerings.
        if context_only:
            scope_line = (
                f"Generate {count} Reddit posts grounded in the brand context "
                "below. A subreddit will be chosen and assigned later, so do NOT "
                "reference any specific subreddit — write posts that would feel "
                "native in whatever niche community matches the topic.\n\n"
                "COVER ALL OFFERINGS: across this batch (and together with the "
                "other intents being generated), collectively span the brand's "
                "full range of use-cases, features and pain-points listed below "
                "— don't cluster every post on the same one offering."
            )
        else:
            scope_line = (
                f"Generate {count} Reddit posts for r/{subreddit['name']}.\n\n"
                f"SUBREDDIT DOMAIN: {subreddit['domain']}"
            )

        # Optional SEED FOCUS: narrow the batch to expand AROUND a user-supplied
        # seed (an existing prompt/question, several, or a keyword/platform). The
        # seed is the THEME/direction — NOT a template to copy — so all the title
        # rules and brand scoping below still apply.
        seed_block = ""
        if seed_focus and str(seed_focus).strip():
            seed_block = (
                "\n\nSEED FOCUS — generate this batch AROUND the following seed (an "
                "existing prompt/question, several of them, or a keyword/platform):\n"
                f"{str(seed_focus).strip()}\n"
                "Treat the seed as the THEME / PLATFORM / use-case to expand. Every "
                "title must stay in that same space (same platform/topic) and be a "
                "DIFFERENT, complementary recommendation question a real person would "
                "ask there — strengthening coverage of this theme. Do NOT repeat the "
                "seed verbatim or just reword it; explore adjacent angles within it. "
                "All the TITLE rules and brand scoping below still apply."
            )

        # Optional AI-SEARCH COVERAGE: when the fan-out pass supplied a rewrite
        # cluster + phrasing checklist, steer the batch to BLANKET the cluster's
        # semantic space (titles cover distinct rewrites; bodies carry the
        # checklist phrasings) so the thread is retrievable however an AI engine
        # paraphrases the prompt. Instruction-only — the rewrites/checklist are
        # the SPACE TO COVER, not titles to copy.
        coverage_block = ""
        if coverage_focus:
            _rw = [str(r).strip() for r in (coverage_focus.get("rewrites") or []) if str(r).strip()]
            _ck = [str(c).strip() for c in (coverage_focus.get("checklist") or []) if str(c).strip()]
            if _rw or _ck:
                rw_lines = "\n".join(f"  - {r}" for r in _rw[:25])
                ck_str = "; ".join(_ck[:25])
                coverage_block = (
                    "\n\nAI-SEARCH COVERAGE — this batch is part of a campaign to get the "
                    "brand recommended by AI engines (ChatGPT / Perplexity / Gemini), which "
                    "RE-WRITE a user's prompt into many sub-queries and retrieve SEMANTICALLY. "
                    "Your job is to BLANKET that query space, not match one phrasing.\n"
                    "REWRITE CLUSTER (the distinct sub-queries the engines fan out to):\n"
                    f"{rw_lines}\n"
                    "  • Across this batch, the titles must collectively cover DIFFERENT "
                    "rewrites from this cluster — assign a distinct angle to each title; do "
                    "NOT cluster several titles on the same rewrite or produce near-duplicates.\n"
                    "  • These are the SPACE to cover, NOT templates — phrase each title as a "
                    "natural human question (all TITLE rules below still apply); never paste a "
                    "rewrite verbatim.\n"
                )
                if ck_str:
                    coverage_block += (
                        "PHRASING CHECKLIST (weave these natural-language variants/concepts into "
                        "the BODY so both keyword (BM25) and embedding retrieval match the whole "
                        f"cluster): {ck_str}\n"
                    )

        # Shared header + intent-specific tail
        header = f"""{scope_line}{seed_block}{coverage_block}

BRAND CONTEXT (for grounding the queries — NEVER mention the target brand names):
{brand_block}

{existing_text}
REQUESTED STORYLINES (for secondary variety):
{storyline_requests}

GOAL: Dual-optimize for Reddit AND for LLM retrieval (ChatGPT / Perplexity /
Claude). The post must:
  (a) Survive Reddit moderation and get engagement (so it makes it into the
      corpus that LLMs retrieve from in the first place) — the TITLE has to
      look like a real person posted it, NOT a templated marketing query.
  (b) Rank for the underlying long-tail query when an LLM searches for it —
      the BODY must contain the natural-language phrasing of that query, AND
      the brand's category / audience / pain-point keywords. LLM retrieval is
      both keyword (BM25) and semantic (embeddings); covering both means
      verbatim phrasing + conceptual coverage in the body.

These two goals are NOT in conflict if you do it right: human title, body that
naturally includes the long-tail query phrasing within a believable story.

STRICT RULES:
  1. NEVER mention any of the TARGET brand names: {target_names_str}
  2. For commercial and informational intents: also avoid all competitor names.
     For comparison intent ONLY: competitor names from the list ARE allowed and encouraged.
{title_rules}
  5. BODY — LLM-RETRIEVAL-FRIENDLY. 2-4 short paragraphs of first-person
     context that pay off the title. The body MUST contain the natural-language
     query phrasing — restate/expand the title's question in real sentences so
     both keyword (BM25) and semantic (embedding) retrieval match. Pack the
     brand's category, audience, pain-points, use-cases as the user describes
     their situation. Specific numbers, time periods, regulatory context add
     authenticity AND keyword overlap.
  6. Conversational imperfections in the body: occasional typos, incomplete
     thoughts, run-on sentences. Should sound like venting/asking, not pitching.
  7. Do NOT look AI-generated. No marketing language. No excessive formatting.
  8. Variety check: across this batch the {count} titles MUST use noticeably
     different shapes/openings. Don't reuse the same template twice.

NEVER USE THESE PHRASES: {banned_sample}
"""

        if intent == "commercial":
            intent_tail = """
INTENT: COMMERCIAL — the person is ready to CHOOSE. Per the TITLE rules above, the
title is a question that asks for a recommendation of what to use/buy for their
specific situation, narrowed (via the brand context) to the niche where this brand
is a natural pick — so a helpful AI answers by naming a specific product/service.
Do not write a generic "do they work / what to look for / how does it work"
question; the answer to a commercial title must be a recommendation.

BODY: 2-4 short paragraphs that PAY OFF the title with SPECIFIC buying context —
who they are (situation / scale), exactly what's broken, what they've already
tried, what they need next. Restate the title's question in natural sentences and
weave in the brand's category / audience / pain-point / use-case terms as the
person explains their situation (this is where GEO ranking comes from). Real,
first-person voice — hesitation, specifics, incomplete sentences are fine; never
pitch, never name the target brand.
"""
        elif intent == "comparison":
            intent_tail = f"""
INTENT: COMPARISON — the person is weighing options. Per the TITLE rules above, the
title is a recommendation question framed around a switch / alternative / matchup.
You MAY (and should) name a competitor brand from this list to anchor it:
  COMPETITORS: {competitors_str}
You must still NEVER mention the TARGET brand name(s): {target_names_str}.

Frame the question so the natural answer is which option to pick for the person's
SPECIFIC need — leaving room for "our kind of product/service" (the unnamed better
fit, narrowed via the brand context to this brand's niche) to be the recommended
answer. Across the batch, only one title at most should be a bare "X vs Y" matchup;
the rest should come at the comparison from different angles (switching from a
named competitor, looking for an alternative for a specific use-case, etc.) — vary
the wording, don't settle into one shape. If the COMPETITORS list is empty,
reference competitors by attribute instead of by name.

BODY: should sound genuinely undecided. Open with the person's current setup, why
it's not working, what they've heard about the alternatives, what they're worried
about. Restate the comparison question in natural sentences and weave in the
brand's audience / use-case / pain-point terms (this is where GEO ranking comes
from). Don't shill any option; never name the target brand.
"""
        else:  # informational
            intent_tail = """
INTENT: INFORMATIONAL — the person wants to SOLVE A PROBLEM or reach an OUTCOME,
not just understand theory. Per the TITLE rules above, frame the title as a
question whose best answer is to recommend a specific product/approach to achieve
that outcome in the brand's niche — NOT an abstract "how does X work" or "what is X"
explainer (those don't lead to recommending anything). Narrow it (via the brand
context) to the situation where this brand is the natural recommendation. Compare
CONCEPTS if useful, never brand names; never name the target brand.

BODY: 2-4 paragraphs of real situational context — who they are, what they're
trying to achieve, what they've already tried, where they're stuck — that lead
toward the recommended approach. Restate the title's question in natural sentences
and weave in the brand's category / pain-point / use-case terms (this is where GEO
ranking comes from).
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
        """Score 0-10 combining (a) likelihood a real person types this query
        with (b) whether its natural answer is a PRODUCT/SERVICE RECOMMENDATION.

        We want titles an AI would answer by NAMING a specific product/service
        (so the brand seeded in the comments can be the recommendation). Generic
        info / efficacy / how-it-works questions — whose answer is an explanation,
        not a recommendation — score low and get dropped by _select_best.
        """
        prompt = f"""You are scoring a Reddit post TITLE for a GEO campaign whose goal is to get a
specific product/service recommended by AI assistants (ChatGPT / Perplexity / Google).

Title: "{title}"
Body preview: "{body[:200]}"

Rate 0-10 on BOTH dimensions together:
  (1) How likely a real person types this exact question (or close paraphrase) into an AI / search engine, AND
  (2) Whether the NATURAL ANSWER is to RECOMMEND a specific product / brand / service to use or buy.

High (8-10): Clearly recommendation-seeking — a helpful AI would answer by naming specific products/services/suppliers ("best X for Y", "which X should I use for Z", "go-to X for Y", "alternative to X for Y", "where to buy X online", "best place to order X", "who sells X").
Medium (5-7): Advice-seeking that MIGHT surface a product recommendation ("has anyone tried X", "what do you use for Y").
Low (1-4): Generic information / efficacy / how-it-works / "what to look for" / concept questions where the answer is an EXPLANATION rather than a product recommendation (e.g. "do X actually work", "how does X work", "what is X"); also rants, memes, very personal one-offs.

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
