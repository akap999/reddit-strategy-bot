"""Subreddit name generation and info creation with availability checking."""

import requests
import time
from generators.base import ClaudeClient
from config import REDDIT_USER_AGENT


class SubredditGenerator:
    def __init__(self, claude: ClaudeClient, reddit_base=None):
        self.claude = claude
        self.reddit_base = reddit_base or "https://www.reddit.com"
        self.headers = {"User-Agent": REDDIT_USER_AGENT}

    def check_availability(self, name):
        """Check if a subreddit name is available on Reddit.
        Returns True if the name is available (subreddit doesn't exist).
        """
        try:
            # Use bot-style UA — Reddit blocks browser-like UAs on JSON API with 403
            headers = dict(self.headers)
            ua = headers.get("User-Agent", "")
            if "Mozilla" in ua or "AppleWebKit" in ua:
                headers["User-Agent"] = "SubredditStrategyBot/2.0 (by /u/strategy_bot_admin)"
            resp = requests.get(
                f"{self.reddit_base}/r/{name}/about.json",
                headers=headers,
                timeout=10
            )
            # 404 = doesn't exist = available
            if resp.status_code == 404:
                return True
            # 200 = need to inspect the response carefully
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except (ValueError, Exception):
                    return None  # invalid JSON — can't determine

                # Reddit returns kind=t5 for real subreddits
                if data.get("kind") == "t5":
                    return False

                # Some responses wrap in {"data": {...}} without "kind"
                sub_data = data.get("data", {})
                if isinstance(sub_data, dict) and sub_data.get("display_name"):
                    # Verify it's actually the subreddit we asked for (not a redirect)
                    display = sub_data["display_name"].lower()
                    if display == name.lower():
                        return False
                    # Reddit redirected to a different sub — original name may be available
                    # but treat as taken to be safe
                    return False

                # Reddit returned 200 but no subreddit data — this is a search/listing page
                # which means the subreddit doesn't exist
                kind = data.get("kind", "")
                if kind == "Listing" or kind == "listing":
                    return True

                # If there's no meaningful subreddit data, assume available
                if not sub_data or not sub_data.get("display_name"):
                    return True

                return None  # ambiguous
            # 403 = could be private/quarantined (taken) or Reddit blocking us
            if resp.status_code == 403:
                try:
                    body = resp.json()
                    reason = body.get("reason", "")
                    if reason in ("private", "quarantined", "banned"):
                        return False  # genuinely taken
                except Exception:
                    pass
                return None  # can't determine — Reddit may be blocking
            # 302/301 redirect — Reddit sometimes redirects non-existent subs
            if resp.status_code in (301, 302):
                return True
            return None  # unknown status — don't assume available
        except requests.exceptions.RequestException:
            return None  # unknown, couldn't check

    def generate_names(self, brand_names, brand_contexts, count=8):
        """Generate generic, domain-specific subreddit name suggestions.

        Args:
            brand_names: list of brand names as reference samples
            brand_contexts: list of what each brand does
            count: number of names to generate

        Returns:
            list of dicts: [{"name": "...", "description": "...", "reasoning": "...", "available": True/False/None}]
        """
        brands_text = "\n".join(
            f"  - {name}: {ctx}"
            for name, ctx in zip(brand_names, brand_contexts)
        )

        prompt = f"""Generate {count} subreddit name suggestions for a community in the same domain as these brands.

REFERENCE BRANDS (do NOT mention these in the names):
{brands_text}

REQUIREMENTS:
1. Names must be GENERIC and domain-specific — never mention any brand name
2. Names should sound like organic communities real people would create
3. Cover the PROBLEM SPACE, not the solution space (e.g., "OnlineTherapyTalk" not "BestTherapyApps")
4. Think about what search terms users type and name the subreddit around those queries
5. Mix of styles: some descriptive (MensHealthOnline), some community-ish (TRTCommunity), some casual (HormoneHelp)
6. Names should be 1-3 words, CamelCase, no underscores preferred
7. Each name should target a slightly different angle or audience within the domain

NEGATIVE EXAMPLES (do NOT generate names like these):
- PeterMDReviews (contains brand name)
- BestTRTClinics (too commercial/listicle)
- TRTAds (obviously promotional)

POSITIVE EXAMPLES:
- NootropicsDiscussion
- RemoteTherapyHelp
- TestosteroneJourney

Return JSON only:
{{
    "suggestions": [
        {{
            "name": "SubredditName",
            "description": "One-line description of the community",
            "reasoning": "Why this name works for the domain"
        }}
    ]
}}"""

        # Generate extra names so we can filter to only available ones
        generate_count = count * 2
        prompt = prompt.replace(f"Generate {count} subreddit", f"Generate {generate_count} subreddit")

        available = []
        seen_names = set()
        max_rounds = 2

        for round_num in range(max_rounds):
            if len(available) >= count:
                break

            if round_num > 0:
                # Second round: ask for more, excluding already seen names
                exclude_list = ", ".join(seen_names)
                prompt_retry = prompt + f"\n\nDo NOT suggest any of these names (already tried): {exclude_list}"
                result = self.claude.call(prompt_retry, max_tokens=1024, temperature=0.9)
            else:
                result = self.claude.call(prompt, max_tokens=1024, temperature=0.8)

            if not result or "suggestions" not in result:
                continue

            candidates = result["suggestions"][:generate_count]

            print(f"    Checking subreddit availability (round {round_num + 1}, {len(candidates)} candidates)...")
            for s in candidates:
                if s["name"] in seen_names:
                    continue
                seen_names.add(s["name"])
                s["available"] = self.check_availability(s["name"])
                time.sleep(0.5)
                if s["available"] is True:
                    available.append(s)
                    if len(available) >= count:
                        break

        return available[:count]

    def generate_subreddit_info(self, name, domain):
        """Generate all metadata needed for creating a subreddit.

        Args:
            name: chosen subreddit name
            domain: topic domain (e.g., "men's telehealth", "hormone therapy")

        Returns:
            dict with description, rules, sidebar, welcome_message
        """
        prompt = f"""Create all the information needed to set up a new subreddit called r/{name}.

DOMAIN: {domain}

Generate realistic, community-focused content that would make this look like an organic, well-moderated subreddit.

Return JSON only:
{{
    "description": "Public-facing description (2-3 sentences, welcoming tone, mentions what the community is about)",
    "rules": [
        {{
            "title": "Rule title",
            "description": "Detailed rule explanation"
        }}
    ],
    "sidebar": "Sidebar text with community guidelines, useful links section, and FAQ pointers (use Reddit markdown formatting)",
    "welcome_message": "Welcome message for new members (friendly, sets expectations, encourages participation)"
}}

IMPORTANT — The DOMAIN field above may contain brand-specific text. You MUST ignore any company names, locations, brand descriptions, or service-specific details. Extract ONLY the broad topic area (e.g., "men's health", "hormone therapy", "online therapy") and base all content on that.

GUIDELINES:
- Generate 5-7 rules covering: civility, no spam, no medical advice disclaimers, stay on topic, no self-promotion, search before posting
- Description should be warm and inclusive
- Sidebar should feel established and helpful
- Welcome message should be brief and encouraging
- Do NOT mention any specific brand names, company names, or brand descriptions
- Do NOT include specific locations, cities, states, or geographic references
- Do NOT include details about specific companies, their services, or features
- Keep ALL content generic to the broad topic/domain — focus on the community, not any business
- Make it feel like a real community, not a marketing vehicle"""

        result = self.claude.call(prompt, max_tokens=1500, temperature=0.7)
        if not result:
            return None

        # Ensure rules is JSON string for DB storage
        rules = result.get("rules", [])
        if isinstance(rules, list):
            result["rules_json"] = json.dumps(rules)
        else:
            result["rules_json"] = "[]"

        return result


# Needed for rules_json
import json
