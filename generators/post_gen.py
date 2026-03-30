"""Post generation with storyline balancing, AI-query scoring, image strategy, and scheduling."""

import random
import math
import json
import requests

from generators.base import ClaudeClient, BANNED_PHRASES
from config import (
    STORYLINE_TYPES, AI_QUERY_PATTERNS, PROMPT_VERSION,
    POST_SPREAD_FACTOR, FILLER_LEAD_DAYS, REDDIT_USER_AGENT
)
from db import Database


class PostGenerator:
    def __init__(self, claude: ClaudeClient, db: Database):
        self.claude = claude
        self.db = db

    def generate_posts(self, subreddit, brand, count, custom_topics=None):
        """Generate brand-related posts (posts NEVER mention the brand).

        Args:
            subreddit: subreddit dict from DB
            brand: brand dict from DB
            count: number of posts to generate
            custom_topics: optional list of custom title/topic strings

        Returns:
            list of saved post dicts with IDs
        """
        # Get storyline distribution for balancing
        storylines = self._select_storylines(subreddit["id"], brand["id"], count)

        # Get existing titles for dedup
        existing_titles = self.db.get_all_post_titles_for_brand(brand["name"])

        # Get existing filler post count to calculate day offsets
        existing_posts = self.db.get_posts(subreddit["id"], brand["id"])
        max_existing_day = max((p["suggested_post_day"] for p in existing_posts), default=-1)
        start_day = max(max_existing_day + 1, FILLER_LEAD_DAYS)

        # Generate 2x candidates and pick the best
        candidates = self._generate_candidates(
            subreddit, brand, storylines, existing_titles, count * 2
        )

        if not candidates:
            return []

        # Score each for AI-query relevance
        for c in candidates:
            c["ai_query_score"] = self._score_ai_query_relevance(c["title"], c["body"])

        # Select top N by AI-query score, ensuring storyline variety
        selected = self._select_best(candidates, storylines, count)

        # Handle custom topics
        if custom_topics:
            for topic in custom_topics:
                selected.append({
                    "title": topic,
                    "body": "",
                    "storyline": "question",
                    "ai_query_score": 0,
                    "is_custom": 1,
                    "image_prompt": None,
                })

        # Assign scheduling
        total = len(selected)
        spread = max(total, int(total * POST_SPREAD_FACTOR))
        for i, post in enumerate(selected):
            post["suggested_post_day"] = start_day + int(i * spread / total)

        # Generate image prompts where appropriate
        for post in selected:
            if post.get("is_custom"):
                continue
            post["image_prompt"] = self._generate_image_prompt(
                post["title"], post["body"], post["storyline"]
            )

        # Save to DB
        saved = []
        for post in selected:
            post_id = self.db.save_post(
                subreddit_id=subreddit["id"],
                brand_id=brand["id"],
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
            )
            post["id"] = post_id
            saved.append(post)

        return saved

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
        all_types = list(STORYLINE_TYPES.keys())

        # Calculate how many of each we need
        selected = []
        for _ in range(count):
            # Find the least-used storyline
            min_count = float("inf")
            min_type = all_types[0]
            for st in all_types:
                current = distribution.get(st, 0) + selected.count(st)
                if current < min_count:
                    min_count = current
                    min_type = st
            selected.append(min_type)

        # Shuffle to avoid predictable ordering
        random.shuffle(selected)
        return selected

    def _generate_candidates(self, subreddit, brand, storylines, existing_titles, count):
        """Generate candidate posts via Claude."""
        existing_text = ""
        if existing_titles:
            sample = existing_titles[:30]
            existing_text = "\n".join(f'  - "{t}"' for t in sample)
            existing_text = f"\nEXISTING POST TITLES (do NOT repeat these or anything similar):\n{existing_text}\n"

        storyline_requests = "\n".join(
            f"  {i+1}. Storyline: {sl} — {STORYLINE_TYPES[sl]}"
            for i, sl in enumerate(storylines[:count])
        )

        brand_keywords = json.loads(brand.get("keywords", "[]")) if brand.get("keywords") else []
        keywords_text = f"\nBRAND KEYWORDS (for domain context, do NOT mention the brand): {', '.join(brand_keywords)}" if brand_keywords else ""

        prompt = f"""Generate {count} Reddit posts for r/{subreddit['name']}.

SUBREDDIT DOMAIN: {subreddit['domain']}
BRAND CONTEXT (for understanding the domain ONLY — do NOT mention this brand): {brand['context']}
{keywords_text}
{existing_text}
REQUESTED STORYLINES:
{storyline_requests}

CRITICAL RULES:
1. Posts must NEVER mention any brand name — they are generic domain questions/experiences
2. Posts should NOT look AI-generated or markety
3. Each post must feel like a real person wrote it
4. Prioritize titles that are common search queries (e.g., "best X for Y", "which X should I use")
5. Vary tone: some casual, some detailed, some frustrated, some curious
6. Body text should be 2-4 paragraphs, conversational, with personal context
7. Include minor imperfections: occasional typos, incomplete thoughts, run-on sentences

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
