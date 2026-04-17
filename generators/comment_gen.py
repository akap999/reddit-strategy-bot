"""
Refactored comment generator with tree generation and configurable brand mention ratio.
Preserves ALL existing logic from comment_generator.py — personas, structures, tone analysis,
relevance check, validation, anti-detection, pattern fingerprinting.
"""

import random
import time
import json
import re
import requests
from collections import Counter
from bs4 import BeautifulSoup

from generators.base import (
    ClaudeClient, PERSONAS, STRUCTURE_TEMPLATES, BANNED_PHRASES,
    GENERATION_SYSTEM_PROMPTS, FEW_SHOT_POOL, select_few_shot_examples,
)

# Multipliers applied to avg_words to compute per-persona word-count ranges.
# Ensures concrete targets in the prompt instead of vague labels.
LENGTH_MULTIPLIERS = {
    "short":        (0.2, 0.4),    # e.g. 12-24 words when avg=60
    "short-medium": (0.35, 0.6),   # e.g. 21-36 words
    "medium":       (0.6, 1.0),    # e.g. 36-60 words
    "medium-long":  (1.0, 1.5),    # e.g. 60-90 words
    "long":         (1.4, 2.2),    # e.g. 84-132 words
}
from config import (
    PROMPT_VERSION, DEFAULT_BRAND_MENTION_RATIO,
    COMMENT_SPREAD_DAYS, REDDIT_USER_AGENT
)
from db import Database


class CommentGenerator:
    def __init__(self, claude: ClaudeClient, db: Database, reddit_base=None):
        self.claude = claude
        self.db = db
        self.pullpush_url = "https://api.pullpush.io/reddit/search/comment"
        self.reddit_base = reddit_base or "https://www.reddit.com"
        self.headers = {"User-Agent": REDDIT_USER_AGENT}
        self._pattern_history = []

    def _detect_and_store_keywords(self, comment_id, body, brand, mentions):
        """If comment mentions brand, detect matched keywords and store them."""
        if not mentions:
            return
        try:
            keywords = json.loads(brand.get("keywords", "[]")) if brand.get("keywords") else []
        except (json.JSONDecodeError, TypeError):
            keywords = []
        if not keywords:
            return
        import re
        matched = [kw for kw in keywords if re.search(r'\b' + re.escape(kw) + r'\b', body, re.IGNORECASE)]
        if matched:
            self.db.update_matched_keywords(comment_id, json.dumps(matched))

    # ------------------------------------------------------------------
    # Reddit data fetching (preserved from original)
    # ------------------------------------------------------------------

    def extract_post_id(self, url):
        try:
            parts = url.split("/comments/")
            if len(parts) > 1:
                return parts[1].split("/")[0]
        except (AttributeError, IndexError):
            pass
        return None

    def extract_subreddit(self, url):
        try:
            parts = url.split("/r/")
            if len(parts) > 1:
                return parts[1].split("/")[0]
        except (AttributeError, IndexError):
            pass
        return "unknown"

    def fetch_comments(self, post_url, limit=20, max_retries=3):
        """Fetch top comments from a Reddit post. Returns (comments, post_body, is_archived)."""
        post_id = self.extract_post_id(post_url)
        if not post_id:
            print(f"    Could not extract post ID from URL")
            return [], "", False

        comments = []
        post_body = ""
        is_archived = False

        for attempt in range(max_retries):
            try:
                clean_url = post_url.split("?")[0].rstrip("/")
                json_url = f"{clean_url}.json"
                response = requests.get(json_url, headers=self.headers, timeout=30)

                if response.status_code == 429:
                    wait = min(2 ** attempt * 3, 30)
                    print(f"    Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                data = response.json()

                if len(data) > 0:
                    post_data = data[0].get("data", {}).get("children", [{}])[0].get("data", {})
                    post_body = post_data.get("selftext", "")[:1000]
                    is_archived = post_data.get("archived", False)

                if len(data) > 1:
                    comment_data = data[1].get("data", {}).get("children", [])
                    for comment in comment_data[:limit]:
                        if comment.get("kind") != "t1":
                            continue
                        c = comment.get("data", {})
                        body = c.get("body", "")
                        if body in ["[deleted]", "[removed]", ""] or len(body) < 10:
                            continue
                        # Count replies
                        replies_data = c.get("replies", "")
                        num_replies = 0
                        if isinstance(replies_data, dict):
                            num_replies = len(replies_data.get("data", {}).get("children", []))
                        comments.append({
                            "body": body[:600],
                            "score": c.get("score", 0),
                            "author": c.get("author", "unknown"),
                            "id": c.get("id", ""),
                            "permalink": c.get("permalink", ""),
                            "num_replies": num_replies,
                            "created_utc": c.get("created_utc", 0),
                        })
                    if comments:
                        return comments, post_body, is_archived
                break

            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    print(f"    Reddit JSON failed: {str(e)[:50]}")

        # Fallback to Pullpush
        try:
            params = {"link_id": post_id, "size": limit, "sort": "desc", "sort_type": "score"}
            response = requests.get(self.pullpush_url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            for comment in data.get("data", []):
                body = comment.get("body", "")
                if body in ["[deleted]", "[removed]", ""] or len(body) < 10:
                    continue
                comments.append({
                    "body": body[:600],
                    "score": comment.get("score", 0),
                    "author": comment.get("author", "unknown"),
                    "id": comment.get("id", ""),
                    "permalink": comment.get("permalink", ""),
                    "num_replies": 0,  # Pullpush doesn't provide reply counts
                    "created_utc": comment.get("created_utc", 0),
                })
        except requests.exceptions.RequestException as e:
            print(f"    Pullpush failed: {str(e)[:50]}")

        return comments, post_body, is_archived

    def _fetch_url(self, url):
        """Fetch a URL with browser headers, curl fallback, and www prefix retry."""
        import subprocess
        browser_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }

        # Step 1: Try requests library
        try:
            session = requests.Session()
            resp = session.get(url, headers=browser_headers, timeout=15, allow_redirects=True)
            resp.raise_for_status()
            if len(resp.text) > 200:
                print(f"    [requests] OK: {url} ({len(resp.text)} bytes)")
                return resp.text
        except requests.exceptions.RequestException as e:
            print(f"    [requests] Failed for {url}: {e}")

        # Step 2: Try curl with full browser headers
        try:
            result = subprocess.run(
                ["curl", "-sL", "-m", "20",
                 "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                 "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                 "-H", "Accept-Language: en-US,en;q=0.9",
                 "--compressed",
                 url],
                capture_output=True, text=True, timeout=25
            )
            if result.returncode == 0 and len(result.stdout) > 200:
                print(f"    [curl] OK: {url} ({len(result.stdout)} bytes)")
                return result.stdout
        except Exception as e:
            print(f"    [curl] Failed for {url}: {e}")

        return None

    def _extract_page_content(self, html):
        """Extract title, meta description, headings, and paragraphs from HTML."""
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        meta_desc = ""
        for attr in [{"name": "description"}, {"property": "og:description"}, {"name": "twitter:description"}]:
            meta_tag = soup.find("meta", attrs=attr)
            if meta_tag and meta_tag.get("content"):
                meta_desc = meta_tag["content"]
                break

        headings = [h.get_text(strip=True) for h in soup.find_all(["h1", "h2", "h3"])[:10]]
        paragraphs = [p.get_text(strip=True) for p in soup.find_all("p")[:10] if len(p.get_text(strip=True)) > 20]
        first_paragraphs = " ".join(paragraphs)[:1200]

        # Also try extracting from structured data (JSON-LD)
        json_ld_text = ""
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                import json as _json
                ld = _json.loads(script.string)
                if isinstance(ld, dict):
                    json_ld_text = ld.get("description", "") or ld.get("name", "")
                elif isinstance(ld, list) and ld:
                    json_ld_text = ld[0].get("description", "") or ld[0].get("name", "")
            except Exception:
                pass

        return {
            "title": title,
            "meta_desc": meta_desc,
            "headings": headings,
            "paragraphs": first_paragraphs,
            "json_ld": json_ld_text[:300],
            "has_content": bool(title or meta_desc or headings),
        }

    def extract_brand_info(self, domain):
        """Fetch a domain's homepage and use Claude to extract brand info."""
        # Normalize URL
        raw_domain = domain.strip().rstrip("/")
        if raw_domain.startswith("http"):
            url = raw_domain
        else:
            url = f"https://{raw_domain}"

        print(f"    Fetching {url}...")

        # Try fetching the URL (with www. prefix fallback)
        html = self._fetch_url(url)
        if not html:
            # Try with www. prefix
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if not parsed.hostname.startswith("www."):
                www_url = f"{parsed.scheme}://www.{parsed.hostname}{parsed.path or '/'}"
                print(f"    Retrying with www prefix: {www_url}")
                html = self._fetch_url(www_url)

        if not html:
            # Last resort: ask Claude to infer from domain name alone
            print(f"    All fetch attempts failed. Inferring from domain name...")
            return self._infer_brand_from_domain(raw_domain)

        # Extract content from homepage
        content = self._extract_page_content(html)

        # If homepage has minimal content, try /about pages
        if not content["has_content"]:
            for about_path in ["/about", "/about-us", "/company"]:
                about_url = url.rstrip("/") + about_path
                print(f"    Homepage empty, trying {about_url}...")
                about_html = self._fetch_url(about_url)
                if about_html:
                    about_content = self._extract_page_content(about_html)
                    if about_content["has_content"]:
                        # Merge: use about content but keep homepage title if available
                        if content["title"] and not about_content["title"]:
                            about_content["title"] = content["title"]
                        content = about_content
                        break

        if not content["has_content"]:
            print(f"    Page has minimal content, inferring from domain name...")
            return self._infer_brand_from_domain(raw_domain)

        print(f"    Analyzing brand info (title: {content['title'][:60]}...)")
        json_ld_line = f"\nSTRUCTURED DATA: {content['json_ld']}" if content["json_ld"] else ""
        prompt = f"""Analyze this website and extract brand information.

DOMAIN: {raw_domain}
PAGE TITLE: {content['title']}
META DESCRIPTION: {content['meta_desc']}
HEADINGS: {', '.join(content['headings'])}
PAGE CONTENT: {content['paragraphs']}{json_ld_line}

Return JSON only:
{{
    "brand_name": "the brand name",
    "brand_context": "A detailed description (3-5 sentences) covering: what the brand does, who it serves (target audience), key services/products offered, and what makes it different from competitors. Be specific about the problem they solve.",
    "brand_keywords": ["keyword1", "keyword2", "keyword3", "keyword4", "keyword5"]
}}"""

        result = self.claude.call(prompt, max_tokens=800, temperature=0.3)
        if result and result.get("brand_name") and result.get("brand_context"):
            return result

        # If Claude couldn't extract, try inferring from domain
        return self._infer_brand_from_domain(raw_domain)

    def _infer_brand_from_domain(self, domain):
        """Last resort: ask Claude to infer brand info from just the domain name."""
        clean = domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
        print(f"    Inferring brand from domain name: {clean}")
        prompt = f"""Based solely on the domain name "{clean}", infer the likely brand information.
Use your knowledge of known brands and common domain naming patterns.

Return JSON only:
{{
    "brand_name": "the likely brand name (extract from domain, e.g. 'getpetermd.com' → 'PeterMD')",
    "brand_context": "Your best guess (3-5 sentences) covering: what the brand likely does, who it serves, key services/products, and what makes it different. If uncertain, provide a reasonable description based on the domain name.",
    "brand_keywords": ["keyword1", "keyword2", "keyword3", "keyword4", "keyword5"]
}}"""

        result = self.claude.call(prompt, max_tokens=512, temperature=0.3)
        if result and result.get("brand_name"):
            return result
        return None

    # ------------------------------------------------------------------
    # Analysis methods (preserved from original)
    # ------------------------------------------------------------------

    def _compute_comment_stats(self, comments):
        if not comments:
            return {"avg_chars": 200, "avg_words": 40, "median_chars": 200, "min_chars": 50, "max_chars": 500, "count": 0}

        lengths_chars = [len(c["body"]) for c in comments]
        lengths_words = [len(c["body"].split()) for c in comments]
        sorted_chars = sorted(lengths_chars)
        mid = len(sorted_chars) // 2
        median_chars = sorted_chars[mid] if len(sorted_chars) % 2 != 0 else (sorted_chars[mid - 1] + sorted_chars[mid]) // 2

        return {
            "avg_chars": sum(lengths_chars) // len(lengths_chars),
            "avg_words": sum(lengths_words) // len(lengths_words),
            "median_chars": median_chars,
            "min_chars": min(lengths_chars),
            "max_chars": max(lengths_chars),
            "count": len(comments),
        }

    def check_relevance(self, post_title, post_body, subreddit, comments, brand_name, brand_context, brand_keywords=None, brand_service_location=None):
        if not comments:
            return {"score": 0, "disqualified": False, "reason": "No comments to analyze"}

        comments_text = "\n".join([f'- "{c["body"][:250]}"' for c in comments[:10]])
        keywords_text = f"\nBRAND KEYWORDS: {', '.join(brand_keywords)}" if brand_keywords else ""
        post_body_text = f'\nPOST BODY: "{post_body[:500]}"' if post_body else ""

        location_text = ""
        location_disqualifier = ""
        if brand_service_location:
            location_text = f"\nSERVICE LOCATION: {brand_service_location} (the brand only serves this area)"
            location_disqualifier = (
                " Post is specifically asking about, or tied to, a geographic area the brand does not serve — "
                "only disqualify on location if the post clearly names a non-matching region. "
                "If the post has no geographic context, DO NOT disqualify on location."
            )

        prompt = f"""Analyze if this Reddit post is relevant for naturally mentioning a brand.

POST TITLE: "{post_title}"
SUBREDDIT: r/{subreddit}{post_body_text}

TOP COMMENTS:
{comments_text}

BRAND: {brand_name}
WHAT BRAND DOES: {brand_context}{keywords_text}{location_text}

Score 0-10 on these criteria:
1. TOPIC MATCH (0-3)
2. PROBLEM-SOLUTION FIT (0-3)
3. NATURAL FIT (0-2)
4. CONVERSATION OPENING (0-2)

DISQUALIFIERS: Meme/joke post, hostile to brands, brand already mentioned, completely off-topic.{location_disqualifier}

Return JSON only:
{{
    "topic_match": 0-3, "problem_fit": 0-3, "natural_fit": 0-2, "conversation_opening": 0-2,
    "total_score": 0-10, "disqualified": true/false, "disqualify_reason": "",
    "recommendation": "GENERATE" or "SKIP",
    "best_angle": "Brief description of how brand could naturally fit",
    "summary": "One sentence explanation"
}}"""

        result = self.claude.call(prompt, temperature=0.3)
        if not result:
            return {"score": 0, "disqualified": False, "reason": "API error"}

        return {
            "score": result.get("total_score", 0),
            "topic_match": result.get("topic_match", 0),
            "problem_fit": result.get("problem_fit", 0),
            "natural_fit": result.get("natural_fit", 0),
            "conversation_opening": result.get("conversation_opening", 0),
            "disqualified": result.get("disqualified", False),
            "disqualify_reason": result.get("disqualify_reason", ""),
            "recommendation": result.get("recommendation", "SKIP"),
            "best_angle": result.get("best_angle", ""),
            "summary": result.get("summary", ""),
        }

    def analyze_tone(self, post_title, post_body, subreddit, comments, comment_stats):
        if not comments:
            return None

        comments_text = "\n".join([
            f'{i+1}. [Score: {c["score"]}] u/{c["author"]}: "{c["body"]}"'
            for i, c in enumerate(comments[:12])
        ])
        post_body_text = f'\nPOST BODY: """{post_body[:500]}"""' if post_body else ""

        prompt = f"""Study these Reddit comments and produce a style guide for writing comments that blend in.

POST TITLE: "{post_title}"
SUBREDDIT: r/{subreddit}{post_body_text}

EXISTING COMMENTS:
{comments_text}

MEASURED STATS: avg {comment_stats['avg_words']} words, median {comment_stats['median_chars']} chars, range {comment_stats['min_chars']}-{comment_stats['max_chars']} chars

Analyze: formality, humor style, technical level, common phrases, length, vibe, sentence structure, caps, punctuation, emotional tone.

Return JSON only:
{{
    "formality": "", "humor_style": "", "technical_level": "",
    "common_phrases": ["phrase1", "phrase2", "phrase3"],
    "avg_length_words": {comment_stats['avg_words']},
    "target_word_count_range": "X-Y words",
    "overall_vibe": "", "sentence_structure": "",
    "capitalization": "", "punctuation_style": "", "emotional_tone": ""
}}"""

        return self.claude.call(prompt, max_tokens=512, temperature=0.3)

    # ------------------------------------------------------------------
    # Anti-detection: pattern tracking (preserved from original)
    # ------------------------------------------------------------------

    def _extract_pattern_fingerprint(self, comment, brand_name, persona_id, structure_id):
        words = comment.split()
        first_five = " ".join(words[:5]).lower() if len(words) >= 5 else " ".join(words).lower()
        word_count = len(words)
        length_bucket = "short" if word_count < 40 else ("medium" if word_count < 80 else "long")
        return {
            "first_five": first_five,
            "persona": persona_id,
            "structure": structure_id,
            "length_bucket": length_bucket,
        }

    def _build_pattern_avoidance_section(self):
        if not self._pattern_history:
            return ""

        all_history = self._pattern_history
        recent = all_history[-12:]
        global_personas = Counter(p["persona"] for p in all_history)
        global_structures = Counter(p["structure"] for p in all_history)
        recent_openings = Counter(p["first_five"] for p in recent)
        lines = ["\nVARIETY GUIDANCE (avoid repeating patterns from this batch):"]

        top_openings = [o for o, _ in recent_openings.most_common(6)]
        if top_openings:
            lines.append(f"  Recent openings (avoid): {', '.join(repr(o) for o in top_openings)}")

        all_persona_ids = {p["id"] for p in PERSONAS}
        all_structure_ids = {s["id"] for s in STRUCTURE_TEMPLATES}
        unused_personas = all_persona_ids - set(global_personas.keys())
        unused_structures = all_structure_ids - set(global_structures.keys())

        if unused_personas:
            lines.append(f"  Unused personas (try these): {', '.join(list(unused_personas)[:5])}")
        elif len(all_history) > 10:
            least_p = [p for p, _ in global_personas.most_common()[-3:]]
            lines.append(f"  Least used personas: {', '.join(least_p)}")

        if unused_structures:
            lines.append(f"  Unused structures (try these): {', '.join(list(unused_structures)[:4])}")
        elif len(all_history) > 10:
            least_s = [s for s, _ in global_structures.most_common()[-3:]]
            lines.append(f"  Least used structures: {', '.join(least_s)}")

        lines.append("  Write something structurally different from your recent output.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Comment config selection (preserved from original)
    # ------------------------------------------------------------------

    def _select_comment_config(self, tone_analysis, comment_stats, relevance, num_comments):
        tone = tone_analysis or {}
        emotional = (tone.get("emotional_tone", "") + " " + tone.get("overall_vibe", "")).lower()
        formality = tone.get("formality", "").lower()
        technical = tone.get("technical_level", "").lower()
        avg_words = (comment_stats or {}).get("avg_words", 50)

        persona_weights = []
        for p in PERSONAS:
            w = 1.0
            pid = p["id"]
            if any(k in emotional for k in ["supportive", "helpful", "encouraging"]):
                if pid in ("helper", "lurker", "concerned"):
                    w += 2.0
            if any(k in emotional for k in ["skeptical", "cynical", "frustrated", "hostile"]):
                if pid in ("skeptic", "frustrated", "dry_humor", "contrarian"):
                    w += 2.0
            if any(k in technical for k in ["high", "technical", "detailed", "data"]):
                if pid in ("data_nerd", "veteran_terse", "comparer"):
                    w += 2.0
            if any(k in formality for k in ["very informal", "casual", "slang"]):
                if pid in ("veteran_terse", "tangent", "newbie", "dry_humor"):
                    w += 1.5
            if any(k in formality for k in ["semi-formal", "professional", "formal"]):
                if pid in ("helper", "data_nerd", "comparer", "concerned", "professional", "researcher"):
                    w += 1.5
            if avg_words < 30:
                if p["length"] in ("short", "short-medium"):
                    w += 1.0
            elif avg_words > 70:
                if p["length"] in ("long", "medium-long"):
                    w += 1.0
            persona_weights.append(w)

        best_angle = (relevance or {}).get("best_angle", "").lower()
        natural_fit = (relevance or {}).get("natural_fit", 1)
        existing_comment_count = (comment_stats or {}).get("count", 0)
        structure_weights = []
        for s in STRUCTURE_TEMPLATES:
            w = 1.0
            sid = s["id"]
            if avg_words < 30:
                if sid in ("short_punchy", "direct_answer"):
                    w += 2.0
                if sid in ("story_arc", "comparison", "anecdote"):
                    w -= 0.5
            elif avg_words > 70:
                if sid in ("story_arc", "comparison", "tangent_drift", "anecdote"):
                    w += 2.0
                if sid in ("short_punchy",):
                    w -= 0.5
            if any(k in best_angle for k in ["question", "asking", "advice"]):
                if sid in ("direct_answer", "question_plus_experience"):
                    w += 1.5
            if any(k in best_angle for k in ["compar", "alternative", "option", "switch"]):
                if sid in ("comparison", "list_format"):
                    w += 1.5
            if any(k in best_angle for k in ["experience", "story", "journey"]):
                if sid in ("story_arc", "anecdote", "update_post"):
                    w += 1.5
            # "update_post" requires existing advice in the thread to follow up on.
            # Zero it out when the post has fewer than 3 existing comments to keep the
            # model from inventing prior suggestions that don't exist.
            if sid == "update_post" and existing_comment_count < 3:
                w = 0.0
            w = max(w, 0.3) if sid != "update_post" or existing_comment_count >= 3 else 0.0
            structure_weights.append(w)

        # Deduplicate against recent history
        recent_personas = set()
        recent_structures = set()
        if self._pattern_history:
            lookback = self._pattern_history[-8:]
            recent_personas = {p["persona"] for p in lookback}
            recent_structures = {p["structure"] for p in lookback}
        for i, p in enumerate(PERSONAS):
            if p["id"] in recent_personas:
                persona_weights[i] *= 0.3
        for i, s in enumerate(STRUCTURE_TEMPLATES):
            if s["id"] in recent_structures:
                structure_weights[i] *= 0.3

        # Weighted selection
        selected_personas = []
        remaining_p_indices = list(range(len(PERSONAS)))
        for _ in range(num_comments):
            if not remaining_p_indices:
                break
            chosen = random.choices(remaining_p_indices, weights=[persona_weights[i] for i in remaining_p_indices], k=1)[0]
            selected_personas.append(PERSONAS[chosen])
            remaining_p_indices.remove(chosen)

        selected_structures = []
        remaining_s_indices = list(range(len(STRUCTURE_TEMPLATES)))
        for _ in range(num_comments):
            if not remaining_s_indices:
                break
            chosen = random.choices(remaining_s_indices, weights=[structure_weights[i] for i in remaining_s_indices], k=1)[0]
            selected_structures.append(STRUCTURE_TEMPLATES[chosen])
            remaining_s_indices.remove(chosen)

        # Per-comment angle hints
        per_comment_angles = []
        base_angle = (relevance or {}).get("best_angle", "")
        if num_comments >= 2:
            per_comment_angles.append(f"Focus on the OP's post: {base_angle}" if base_angle else "Respond to the OP's main question/concern")
            best_angle_text = (relevance or {}).get("best_angle", "").lower()
            if any(k in best_angle_text for k in ["question", "asking", "advice", "help"]):
                second_angle = "Give practical advice on the specific question being asked"
            elif any(k in best_angle_text for k in ["compar", "option", "alternative", "switch"]):
                second_angle = "Compare a few options you have looked into, give honest pros/cons"
            elif any(k in emotional for k in ["frustrated", "skeptic", "cynical"]):
                second_angle = "Share a different perspective from the majority"
            elif any(k in emotional for k in ["supportive", "helpful"]):
                second_angle = "Add a detail or tip that nobody else in the thread mentioned"
            elif natural_fit >= 2:
                second_angle = "Focus on a specific detail in the post that others overlooked"
            else:
                second_angle = "Give practical advice based on your own situation"
            per_comment_angles.append(second_angle)
            for _ in range(num_comments - 2):
                per_comment_angles.append(base_angle or "Find a unique angle into this conversation")
        else:
            per_comment_angles = [base_angle or "Respond to the OP's main question/concern"]

        return selected_personas, selected_structures, per_comment_angles

    def _select_reply_target(self, comments, post_title, brand_name, relevance, min_score=0):
        """Select a comment worth replying to. Returns None if nothing scores above min_score."""
        if not comments:
            return None
        best_angle = (relevance or {}).get("best_angle", "").lower()
        brand_lower = brand_name.lower()
        angle_words = set(best_angle.split()) - {"the", "a", "an", "is", "are", "to", "for", "and", "or", "with", "in", "on", "of"}

        scored = []
        for c in comments:
            if c["author"].lower() in ("automoderator", "[deleted]", "unknown", "bot"):
                continue
            if len(c["body"]) < 20:
                continue
            if brand_lower in c["body"].lower():
                continue
            score = 0.0
            comment_score = max(c.get("score", 1), 1)
            score += min(comment_score, 50)
            body_lower = c["body"].lower()
            overlap = sum(1 for w in angle_words if w in body_lower)
            score += overlap * 5
            if "?" in c["body"]:
                score += 10
            word_count = len(c["body"].split())
            if 20 <= word_count <= 100:
                score += 5
            scored.append((score, c))

        if not scored:
            return None

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_comment = scored[0]
        if best_score < min_score:
            return None
        return best_comment

    # ------------------------------------------------------------------
    # Core generation (adapted from original — now supports mention_brand flag)
    # ------------------------------------------------------------------

    def generate_comments(self, post_title, post_body, subreddit, comments,
                          brand_name, brand_context, best_angle="", num_comments=2,
                          tone_analysis=None, comment_stats=None, retry_feedback=None,
                          relevance=None, reply_targets=None, mention_brand_flags=None,
                          brand_assignments=None, all_brand_names=None,
                          brand_service_location=None):
        """Generate comments. mention_brand_flags is a list of bools per comment index.

        For multi-brand: brand_assignments is a list where each element is None (organic)
        or a brand dict (mention that specific brand). all_brand_names lists all brand
        names to avoid in organic comments.
        """

        if not comments and not post_body:
            return {"generated_comments": [], "strategies_used": [], "_personas": [], "_structures": []}

        comments_text = ""
        if comments:
            comments_text = "\n".join([
                f'{i+1}. [Score: {c["score"]}] u/{c["author"]}: "{c["body"]}"'
                for i, c in enumerate(comments[:15])
            ])

        post_body_text = f'\nPOST BODY: """{post_body[:800]}"""' if post_body else ""

        selected_personas, selected_structures, per_comment_angles = \
            self._select_comment_config(tone_analysis, comment_stats, relevance, num_comments)

        few_shot_text = select_few_shot_examples(n=3)

        # Default: all comments mention brand (legacy behavior)
        if mention_brand_flags is None:
            mention_brand_flags = [True] * num_comments

        # Build the "do not mention" brand list
        avoid_brands = ", ".join(all_brand_names) if all_brand_names else brand_name

        reply_targets = reply_targets or {}
        comment_instructions = []
        for idx in range(num_comments):
            persona = selected_personas[idx] if idx < len(selected_personas) else random.choice(PERSONAS)
            structure = selected_structures[idx] if idx < len(selected_structures) else random.choice(STRUCTURE_TEMPLATES)
            angle = per_comment_angles[idx] if idx < len(per_comment_angles) else ""
            should_mention = mention_brand_flags[idx] if idx < len(mention_brand_flags) else False
            angle_line = f"\n    ANGLE: {angle}" if angle else ""

            reply_line = ""
            if idx in reply_targets:
                target = reply_targets[idx]
                reply_line = (
                    f'\n    TARGET COMMENT by u/{target["author"]}: "{target["body"][:400]}"'
                    f"\n    Write as if you clicked 'reply' on their comment. Respond to what THEY said specifically."
                )

            # Multi-brand: use per-comment brand assignment if available
            assigned = None
            if brand_assignments and idx < len(brand_assignments):
                assigned = brand_assignments[idx]

            brand_line = ""
            if should_mention and assigned:
                # Multi-brand: mention the specific assigned brand
                brand_line = f"\n    BRAND: Mention {assigned['name']} exactly once as a brief aside."
                ctx = assigned.get("context", "")
                if ctx:
                    brand_line += f"\n    BRAND CONTEXT (use this to make the mention relevant and natural): {ctx}"
                loc = assigned.get("service_location") or brand_service_location
                if loc:
                    brand_line += f"\n    SERVICE LOCATION: {loc} — only reference geography if the post already has geographic context."
            elif should_mention:
                # Single-brand fallback
                brand_line = f"\n    BRAND: Mention {brand_name} exactly once as a brief aside."
                if brand_context:
                    brand_line += f"\n    BRAND CONTEXT (use this to make the mention relevant and natural): {brand_context}"
                if brand_service_location:
                    brand_line += f"\n    SERVICE LOCATION: {brand_service_location} — only reference geography if the post already has geographic context."
            else:
                brand_line = f"\n    BRAND: Do NOT mention {avoid_brands} or any brand in this comment."

            avg_w = (comment_stats or {}).get("avg_words", 60)
            lo_m, hi_m = LENGTH_MULTIPLIERS.get(persona['length'], (0.6, 1.0))
            lo_w, hi_w = max(8, int(avg_w * lo_m)), int(avg_w * hi_m)

            comment_instructions.append(
                f"  Comment {idx+1}:\n"
                f"    PERSONA: {persona['voice']}\n"
                f"    STRUCTURE: {structure['instruction']}\n"
                f"    LENGTH: {lo_w}-{hi_w} words ({persona['length']}){brand_line}{angle_line}{reply_line}"
            )
        per_comment_section = "\n".join(comment_instructions)

        # Build tone section
        if tone_analysis:
            tone_section = f"""
TONE ANALYSIS (match this style):
  Formality: {tone_analysis.get('formality', 'unknown')}
  Humor: {tone_analysis.get('humor_style', 'unknown')}
  Technical level: {tone_analysis.get('technical_level', 'unknown')}
  Common phrases: {', '.join(tone_analysis.get('common_phrases', []))}
  Vibe: {tone_analysis.get('overall_vibe', 'unknown')}
  Sentence style: {tone_analysis.get('sentence_structure', 'unknown')}
  Caps: {tone_analysis.get('capitalization', 'unknown')}
  Punctuation: {tone_analysis.get('punctuation_style', 'unknown')}
  Emotional tone: {tone_analysis.get('emotional_tone', 'unknown')}"""
        else:
            tone_section = "\nTONE: Match the exact style of the existing comments above."

        # Length constraint — each comment gets its own word-count target via
        # per-comment LENGTH lines. The global section just reinforces variety.
        length_section = "\nLENGTH: Each comment has its own word-count target in its assignment below. Some MUST be very short (1-2 sentences) and others much longer. Do NOT make all comments similar length — length variety is critical."

        retry_section = ""
        if retry_feedback:
            retry_section = f"\nPREVIOUS ATTEMPT FAILED — fix these issues:\n{retry_feedback}\nDo NOT repeat the same mistakes."

        pattern_avoidance = self._build_pattern_avoidance_section()
        banned_sample = random.sample(BANNED_PHRASES, min(10, len(BANNED_PHRASES)))
        banned_text = ", ".join(f'"{p}"' for p in banned_sample)

        # Determine which comments mention brand for the brand rules section
        brand_comment_indices = [i+1 for i, f in enumerate(mention_brand_flags[:num_comments]) if f]
        no_brand_indices = [i+1 for i, f in enumerate(mention_brand_flags[:num_comments]) if not f]

        brand_rules = ""
        if brand_comment_indices:
            brand_rules = f"""
BRAND RULES (for comments {brand_comment_indices} ONLY — other comments must NOT mention any brand):

RULE #1: After you write "{brand_name}", IMMEDIATELY change topic. Do NOT continue talking about it.
Do NOT use "they", "them", "their", "it" to refer back to the brand. Do NOT describe what they do.

Other brand rules for brand-mentioning comments:
- Mention {brand_name} exactly once
- For 3+ sentence comments: brand NEVER in first two sentences or last sentence
- For 1-2 sentence comments: brand mid-sentence in parenthetical only
- Be neutral and brief — just name-drop and move on
- Do NOT set up a negative experience then introduce {brand_name} as the solution
- If you mention frustration with alternatives, also mention a downside of {brand_name}

NEVER USE THESE PHRASES: {banned_text}"""

        existing_comments_section = ""
        if comments_text:
            existing_comments_section = f"\nEXISTING COMMENTS:\n{comments_text}"

        prompt = f"""You're commenting in a Reddit thread about a topic you know well.

POST: "{post_title}"
SUBREDDIT: r/{subreddit}{post_body_text}
{existing_comments_section}
{tone_section}
{length_section}

EACH COMMENT HAS A UNIQUE ASSIGNMENT:
{per_comment_section}
{brand_rules}
{pattern_avoidance}

COMMENT QUALITY RULES:
- Only reference things that actually appear in the POST BODY or EXISTING COMMENTS above. Do NOT invent prior suggestions, advice, attempts, updates, or thread history that isn't written there.
- If there are no existing comments (or they contain no advice/suggestions), do NOT write as if you're responding to other commenters or "what people said" / "what people suggested". Respond only to the post itself.
- Reference something specific from THIS post
- Each comment must be structurally different
- Your comment MUST be valuable even without brand mentions
- Do NOT open two comments the same way
- Vary punctuation naturally
- Write like a casual Reddit user — ramble, incomplete thoughts, not too polished
- NEVER use dashes (-), em-dashes (—), or double-dashes (--) anywhere in the comment. Use commas, periods, or restructure the sentence instead.
{retry_section}

{few_shot_text}

Generate exactly {num_comments} comments. Return JSON only:
{{
    "generated_comments": ["comment 1", "comment 2", ...],
    "strategies_used": ["strategy 1", "strategy 2", ...]
}}"""

        temperature = 0.95 if retry_feedback else 0.9
        system_prompt = random.choice(GENERATION_SYSTEM_PROMPTS)
        max_tok = 2000 if num_comments <= 2 else 3000
        result = self.claude.call(prompt, max_tokens=max_tok, temperature=temperature, system_prompt=system_prompt)

        if not result:
            return {"generated_comments": [], "strategies_used": [], "_personas": [], "_structures": []}

        result["_personas"] = [p["id"] for p in selected_personas[:num_comments]]
        result["_structures"] = [s["id"] for s in selected_structures[:num_comments]]
        return result

    # ------------------------------------------------------------------
    # Validation (preserved from original)
    # ------------------------------------------------------------------

    def validate_comments(self, post_title, post_body, subreddit, comments,
                          brand_name, generated_comments, tone_analysis=None):
        if not generated_comments:
            return {"evaluations": [], "any_failed": True}

        real_comments_text = "\n".join([
            f'- u/{c["author"]}: "{c["body"][:200]}"' for c in (comments or [])[:5]
        ])
        gen_text = "\n".join([
            f'Comment {i+1}: """{comment}"""' for i, comment in enumerate(generated_comments)
        ])
        tone_section = ""
        if tone_analysis:
            tone_section = f"\nEXPECTED TONE: {tone_analysis.get('formality', '?')} | {tone_analysis.get('overall_vibe', '?')}"

        prompt = f"""Evaluate if these generated Reddit comments would pass as genuine.

ORIGINAL POST: "{post_title}" in r/{subreddit}
Body: {(post_body or '')[:500]}

REAL COMMENTS: {real_comments_text}
{tone_section}

BRAND: {brand_name}

GENERATED COMMENTS:
{gen_text}

Score each on: AUTHENTICITY (1-10), POST RELEVANCE (1-10), TONE MATCH (1-10).
Check for: marketing_language (banned phrases), structural_promotion (testimonial patterns).
PASS: avg scores >= 7 AND no marketing language AND no structural promotion.

Return JSON only:
{{
    "evaluations": [
        {{
            "comment_index": 0, "authenticity_score": 1-10, "post_relevance_score": 1-10,
            "tone_match_score": 1-10, "marketing_language": true/false,
            "marketing_phrases_found": [], "structural_promotion": true/false,
            "structural_promotion_reason": "", "overall_score": 0,
            "pass": true/false, "feedback": ""
        }}
    ],
    "any_failed": true/false
}}"""

        result = self.claude.call(prompt, max_tokens=1024, temperature=0.2)
        if not result:
            return {"evaluations": [], "any_failed": False}

        # Programmatic checks (preserved from original)
        evals = result.get("evaluations", [])
        brand_lower = brand_name.lower()

        for ev_idx, ev in enumerate(evals):
            if ev_idx >= len(generated_comments):
                continue
            comment_lower = generated_comments[ev_idx].lower()

            # Banned phrase check
            found = [p for p in BANNED_PHRASES if p in comment_lower]
            if found:
                ev["marketing_language"] = True
                ev["marketing_phrases_found"] = found
                ev["pass"] = False
                ev["feedback"] = f"Marketing phrases detected: {found}. " + ev.get("feedback", "")

            # Dash check — no dashes of any kind allowed
            if re.search(r'[\u2013\u2014]| - |--', generated_comments[ev_idx]):
                ev["pass"] = False
                ev["feedback"] = "Contains dashes (-, —, --). Rewrite without any dashes. " + ev.get("feedback", "")

            # Brand sentence ratio check
            if brand_lower in comment_lower:
                sentences = comment_lower.replace('!', '.').replace('?', '.').split('.')
                sentences = [s.strip() for s in sentences if s.strip()]
                brand_sentences = sum(1 for s in sentences if brand_lower in s)
                if sentences and brand_sentences / len(sentences) > 0.3:
                    ev["pass"] = False
                    ev["structural_promotion"] = True
                    ev["feedback"] = f"Brand in {brand_sentences}/{len(sentences)} sentences. " + ev.get("feedback", "")

            # Brand continuation check
            _continuation_patterns = [
                f"{brand_lower} and they", f"{brand_lower} and their",
                f"{brand_lower} since they", f"{brand_lower} because they",
                f"{brand_lower} who ", f"{brand_lower} where they",
                f"{brand_lower} which ", f"{brand_lower} that ",
                f"{brand_lower} and it", f"{brand_lower} and the ",
                f"{brand_lower}, they", f"{brand_lower}, which",
                f"{brand_lower}, and they",
            ]
            if brand_lower in comment_lower:
                for pat in _continuation_patterns:
                    if pat in comment_lower:
                        ev["pass"] = False
                        ev["feedback"] = f"Brand continuation detected ('{pat}'). " + ev.get("feedback", "")
                        break

            # Problem-then-brand-solution arc check
            _negative_precursors = [
                "useless", "terrible", "awful", "horrible", "worst", "sucked",
                "dismissing", "dismissed", "ignored", "brushed off", "wouldn't listen",
                "waste of time", "waste of money", "rip off", "scam",
                "sick of", "fed up", "frustrated", "gave up", "out of options",
                "didn't work", "wasn't working", "stopped working", "failed",
                "too expensive", "overcharged", "nickel and dime",
                "long wait", "took forever", "weeks to", "months to",
                "switched from", "left my", "ditched", "dropped",
            ]
            if brand_lower in comment_lower:
                brand_pos = comment_lower.find(brand_lower)
                pre_brand = comment_lower[max(0, brand_pos - 150):brand_pos]
                found_negatives = [n for n in _negative_precursors if n in pre_brand]
                if found_negatives:
                    ev["pass"] = False
                    ev["feedback"] = f"Problem-then-brand arc detected ({', '.join(found_negatives[:3])}). " + ev.get("feedback", "")

        result["any_failed"] = any(not ev.get("pass") for ev in evals)
        return result

    # ------------------------------------------------------------------
    # NEW: Comment tree generation for fresh posts
    # ------------------------------------------------------------------

    def generate_comment_tree(self, post, brand_or_brands, num_comments,
                               brand_mention_ratio=None, post_day_offset=0,
                               brands_config=None, op_reply_count=0):
        """Generate a full comment tree for a fresh post (no existing Reddit comments).

        Args:
            post: post dict from DB
            brand_or_brands: single brand dict (backward compat) OR ignored if brands_config given
            num_comments: total number of comments (top-level + replies + OP replies)
            brand_mention_ratio: fraction of comments that mention brand (single-brand mode)
            post_day_offset: the day the post is scheduled for
            brands_config: list of {"brand": brand_dict, "mention_count": int} for multi-brand
            op_reply_count: number of OP replies to include in the tree

        Returns:
            list of saved comment dicts with IDs and tree structure
        """
        # Normalize into brands_config format
        if brands_config is None:
            # Single-brand backward compat
            brand = brand_or_brands
            if brand_mention_ratio is None:
                brand_mention_ratio = DEFAULT_BRAND_MENTION_RATIO
            mention_count = round(num_comments * brand_mention_ratio)
            if brand_mention_ratio > 0:
                mention_count = max(1, mention_count)  # at least 1 if ratio > 0
            brands_config = [{"brand": brand, "mention_count": mention_count}]

        # Build per-comment brand assignments: None = organic, brand_dict = mention that brand
        mention_assignments = [None] * num_comments
        brand_slots = []
        for bc in brands_config:
            for _ in range(bc["mention_count"]):
                brand_slots.append(bc["brand"])

        # Assign brand slots to random comment indices (skip index 0 — too obvious)
        available = list(range(1, num_comments)) if num_comments > 1 else [0]
        random.shuffle(available)
        for i, assigned_brand in enumerate(brand_slots):
            if i < len(available):
                mention_assignments[available[i]] = assigned_brand

        # Primary brand for fallback/dedup (first in config)
        primary_brand = brands_config[0]["brand"]
        all_brand_names = list(set(bc["brand"]["name"] for bc in brands_config))

        # Determine tree shape: ~80% top-level, ~20% replies (minus OP replies)
        non_op_count = num_comments - op_reply_count
        num_top = max(1, int(non_op_count * 0.8))
        num_replies = non_op_count - num_top

        # Build mention flags (True/False for legacy generate_comments compatibility)
        mention_flags = [ma is not None for ma in mention_assignments]

        # Generate tone analysis from subreddit description as context
        subreddit = self.db.get_subreddit(post["subreddit_id"])
        mock_tone = {
            "formality": "casual to semi-formal",
            "humor_style": "occasional dry humor",
            "technical_level": "moderate",
            "common_phrases": [],
            "overall_vibe": "helpful community discussion",
            "sentence_structure": "mix of short and medium",
            "capitalization": "mostly lowercase with normal caps",
            "punctuation_style": "casual, minimal",
            "emotional_tone": "generally supportive",
        }

        mock_stats = {"avg_chars": 300, "avg_words": 60, "median_chars": 250, "min_chars": 50, "max_chars": 600}

        # Get existing comment bodies for dedup (union across all brands)
        existing_bodies = []
        for bname in all_brand_names:
            existing_bodies.extend(self.db.get_all_comment_bodies_for_brand(bname, limit=50))
        dedup_text = ""
        if existing_bodies:
            sample = existing_bodies[:20]
            dedup_text = "\nPREVIOUS COMMENTS (do NOT repeat these openings or structures):\n" + \
                "\n".join(f'  - "{b[:80]}..."' for b in sample)

        # Generate top-level comments
        print(f"    Generating {num_top} top-level comments...")
        top_assignments = mention_assignments[:num_top]
        top_level_result = self.generate_comments(
            post_title=post["title"],
            post_body=post["body"],
            subreddit=subreddit["name"],
            comments=[],  # no existing comments
            brand_name=primary_brand["name"],
            brand_context=primary_brand["context"],
            num_comments=num_top,
            tone_analysis=mock_tone,
            comment_stats=mock_stats,
            mention_brand_flags=mention_flags[:num_top],
            relevance={"best_angle": "general discussion", "natural_fit": 2},
            brand_assignments=top_assignments,
            all_brand_names=all_brand_names,
        )

        top_comments = top_level_result.get("generated_comments", [])
        top_personas = top_level_result.get("_personas", [])
        top_structures = top_level_result.get("_structures", [])

        if not top_comments:
            return []

        # Save top-level comments and assign scheduling
        saved = []
        top_ids = []
        for i, body in enumerate(top_comments):
            assigned = top_assignments[i] if i < len(top_assignments) else None
            if assigned:
                mentions = assigned["name"].lower() in body.lower()
                comment_brand_id = assigned["id"]
            else:
                mentions = False
                comment_brand_id = primary_brand["id"]

            # Schedule: first 1-2 comments on post day, rest spread across days
            if i < 2:
                comment_day = post_day_offset
            else:
                comment_day = post_day_offset + 1 + (i - 2) * COMMENT_SPREAD_DAYS // max(num_top - 2, 1)

            # Brand mentions don't appear on day 0
            if mentions and comment_day == post_day_offset and i >= 2:
                comment_day = post_day_offset + 2

            comment_id = self.db.save_comment(
                post_id=post["id"],
                brand_id=comment_brand_id,
                body=body,
                persona_id=top_personas[i] if i < len(top_personas) else None,
                structure_id=top_structures[i] if i < len(top_structures) else None,
                is_reply=0,
                parent_comment_id=None,
                mentions_brand=1 if mentions else 0,
                status="complete",
                suggested_post_day=comment_day,
                suggested_order=i,
                prompt_version=PROMPT_VERSION,
            )
            if assigned:
                self._detect_and_store_keywords(comment_id, body, assigned, mentions)
            saved.append({"id": comment_id, "body": body, "is_reply": False, "mentions_brand": mentions, "day": comment_day})
            top_ids.append(comment_id)

            # Track pattern
            brand_name_for_fp = assigned["name"] if assigned else primary_brand["name"]
            fp = self._extract_pattern_fingerprint(
                body, brand_name_for_fp,
                top_personas[i] if i < len(top_personas) else "unknown",
                top_structures[i] if i < len(top_structures) else "unknown"
            )
            self._pattern_history.append(fp)

        # Generate replies — only reply to existing comments if relevant
        if num_replies > 0:
            reply_assignments = mention_assignments[num_top:num_top + num_replies]
            reply_mention_flags = mention_flags[num_top:num_top + num_replies]

            # Check if post is published and has live comments worth replying to
            reddit_url = self.db.get_url_for_post(post["id"])
            live_comments = []
            if reddit_url:
                print(f"    Fetching live comments to check for reply opportunities...")
                live_comments, _, _ = self.fetch_comments(reddit_url)

            replies_generated = 0
            for r_idx in range(num_replies):
                r_assigned = reply_assignments[r_idx] if r_idx < len(reply_assignments) else None
                should_mention = r_assigned is not None
                reply_brand = r_assigned if r_assigned else primary_brand
                target = None
                parent_comment_id = None
                parent_day = post_day_offset

                # Only reply to live comments if a relevant one exists (min_score=10)
                if live_comments:
                    target = self._select_reply_target(live_comments, post["title"], reply_brand["name"],
                        {"best_angle": "general", "natural_fit": 2}, min_score=10)

                # Fall back to generated comments if no relevant live target
                if not target and top_comments:
                    parent_idx = random.randint(0, len(top_comments) - 1)
                    parent_body = top_comments[parent_idx]
                    target = {"body": parent_body, "score": 5, "author": "community_member", "id": "", "permalink": ""}
                    parent_comment_id = top_ids[parent_idx] if parent_idx < len(top_ids) else None
                    parent_day = saved[parent_idx]["day"] if parent_idx < len(saved) else post_day_offset

                if not target:
                    continue  # nothing relevant to reply to — skip

                reply_day = parent_day + random.randint(1, 3)

                reply_result = self.generate_comments(
                    post_title=post["title"],
                    post_body=post["body"],
                    subreddit=subreddit["name"],
                    comments=[target],
                    brand_name=reply_brand["name"],
                    brand_context=reply_brand.get("context", ""),
                    num_comments=1,
                    tone_analysis=mock_tone,
                    comment_stats=mock_stats,
                    mention_brand_flags=[should_mention],
                    reply_targets={0: target},
                    relevance={"best_angle": "replying to comment", "natural_fit": 2},
                    brand_assignments=[r_assigned],
                    all_brand_names=all_brand_names,
                )

                reply_comments = reply_result.get("generated_comments", [])
                if reply_comments:
                    replies_generated += 1
                    reply_body = reply_comments[0]
                    mentions = should_mention and reply_brand["name"].lower() in reply_body.lower()

                    reply_personas = reply_result.get("_personas", [])
                    reply_structures = reply_result.get("_structures", [])

                    comment_id = self.db.save_comment(
                        post_id=post["id"],
                        brand_id=reply_brand["id"],
                        body=reply_body,
                        persona_id=reply_personas[0] if reply_personas else None,
                        structure_id=reply_structures[0] if reply_structures else None,
                        is_reply=1,
                        parent_comment_id=parent_comment_id,
                        mentions_brand=1 if mentions else 0,
                        status="complete",
                        suggested_post_day=reply_day,
                        suggested_order=r_idx,
                        prompt_version=PROMPT_VERSION,
                    )
                    if r_assigned:
                        self._detect_and_store_keywords(comment_id, reply_body, r_assigned, mentions)
                    saved.append({"id": comment_id, "body": reply_body, "is_reply": True, "mentions_brand": mentions, "day": reply_day, "parent_id": parent_comment_id, "reply_to": target.get("author", "")})

                    if reply_personas:
                        fp = self._extract_pattern_fingerprint(reply_body, reply_brand["name"], reply_personas[0], reply_structures[0] if reply_structures else "unknown")
                        self._pattern_history.append(fp)

            if replies_generated:
                print(f"    Generated {replies_generated} replies (of {num_replies} slots)")

        # Generate OP replies if requested
        if op_reply_count > 0 and top_comments:
            print(f"    Generating {op_reply_count} OP replies...")
            op_saved = self.generate_op_replies(
                post, primary_brand, num_replies=op_reply_count,
                post_day_offset=post_day_offset,
            )
            saved.extend(op_saved)

        return saved

    def generate_hq_comment(self, post, brand, brand_mention_ratio=None, post_day_offset=0):
        """Generate one high-quality top-level comment with brand mention plus 5
        relevant replies forming a realistic nested conversation thread (6 total).

        The main comment (index 0) always mentions the brand. Replies do not.
        Thread shapes are randomized — some replies target the main comment,
        others reply to earlier replies.
        """
        # brand_mention_ratio is ignored — main comment always mentions brand

        # --- Thread shape ---------------------------------------------------
        # Each shape is a list of (index, parent_index) tuples
        shapes = [
            # Shape A: main -> [R1,R2,R3], R1->[R4], R2->[R5]
            [(0, None), (1, 0), (2, 0), (3, 0), (4, 1), (5, 2)],
            # Shape B: main -> [R1,R2,R3], R1->[R4,R5]
            [(0, None), (1, 0), (2, 0), (3, 0), (4, 1), (5, 1)],
            # Shape C: main -> [R1,R2], R1->[R3,R4], R3->[R5]
            [(0, None), (1, 0), (2, 0), (3, 1), (4, 1), (5, 3)],
        ]
        shape = random.choice(shapes)

        # --- Brand mentions --------------------------------------------------
        # Main comment (index 0) always mentions brand, replies never do
        mention_flags = [True, False, False, False, False, False]

        # --- Setup (mirrors generate_comment_tree) ---------------------------
        subreddit = self.db.get_subreddit(post["subreddit_id"])
        mock_tone = {
            "formality": "casual to semi-formal",
            "humor_style": "occasional dry humor",
            "technical_level": "moderate",
            "common_phrases": [],
            "overall_vibe": "helpful community discussion",
            "sentence_structure": "mix of short and medium",
            "capitalization": "mostly lowercase with normal caps",
            "punctuation_style": "casual, minimal",
            "emotional_tone": "generally supportive",
        }

        # Longer avg for HQ depth
        hq_stats = {"avg_chars": 500, "avg_words": 100, "median_chars": 400,
                     "min_chars": 80, "max_chars": 800}
        reply_stats = {"avg_chars": 300, "avg_words": 60, "median_chars": 250,
                       "min_chars": 50, "max_chars": 600}

        existing_bodies = self.db.get_all_comment_bodies_for_brand(brand["name"], limit=100)
        dedup_text = ""
        if existing_bodies:
            sample = existing_bodies[:20]
            dedup_text = ("\nPREVIOUS COMMENTS (do NOT repeat these openings or structures):\n"
                          + "\n".join(f'  - "{b[:80]}..."' for b in sample))

        # Pre-select 6 distinct personas/structures
        all_personas, all_structures, all_angles = self._select_comment_config(
            mock_tone, hq_stats,
            {"best_angle": "detailed thoughtful response", "natural_fit": 3},
            6,
        )

        saved = []          # list of dicts with id, body, etc.
        saved_ids = {}       # index -> DB comment id
        saved_bodies = {}    # index -> body text
        saved_personas = {}  # index -> persona id

        print(f"    Generating HQ comment thread (1 main + 5 replies)...")

        for idx, parent_idx in shape:
            is_main = parent_idx is None

            # Build context from previously generated comments
            thread_comments = [
                {"body": saved_bodies[i], "score": 5,
                 "author": saved_personas.get(i, "community_member"),
                 "id": "", "permalink": ""}
                for i in sorted(saved_bodies.keys())
            ]

            reply_targets = {}
            if not is_main:
                parent_body = saved_bodies[parent_idx]
                parent_target = {
                    "body": parent_body, "score": 5,
                    "author": saved_personas.get(parent_idx, "community_member"),
                    "id": "", "permalink": "",
                }
                reply_targets = {0: parent_target}

            # Pick this comment's persona/structure
            persona_id = all_personas[idx] if idx < len(all_personas) else random.choice(PERSONAS)["id"]
            structure_id = all_structures[idx] if idx < len(all_structures) else random.choice(STRUCTURE_TEMPLATES)["id"]

            result = self.generate_comments(
                post_title=post["title"],
                post_body=post["body"],
                subreddit=subreddit["name"],
                comments=thread_comments,
                brand_name=brand["name"],
                brand_context=brand["context"],
                num_comments=1,
                tone_analysis=mock_tone,
                comment_stats=hq_stats if is_main else reply_stats,
                mention_brand_flags=[mention_flags[idx]],
                reply_targets=reply_targets if reply_targets else None,
                relevance={
                    "best_angle": (
                        "Give a thoughtful, detailed response showing genuine expertise "
                        "with a specific personal experience or concrete example"
                        if is_main
                        else "Respond naturally to the conversation — agree, disagree, "
                             "or add a new angle"
                    ),
                    "natural_fit": 3,
                },
            )

            bodies = result.get("generated_comments", [])
            if not bodies:
                print(f"    Warning: failed to generate comment at index {idx}")
                continue

            body = bodies[0]
            mentions = mention_flags[idx] and brand["name"].lower() in body.lower()

            # Scheduling
            if is_main:
                comment_day = post_day_offset
            elif parent_idx == 0:
                comment_day = post_day_offset + 1
            else:
                comment_day = post_day_offset + 2

            r_personas = result.get("_personas", [])
            r_structures = result.get("_structures", [])
            p_id = r_personas[0] if r_personas else persona_id
            s_id = r_structures[0] if r_structures else structure_id

            comment_id = self.db.save_comment(
                post_id=post["id"],
                brand_id=brand["id"],
                body=body,
                persona_id=p_id,
                structure_id=s_id,
                is_reply=0 if is_main else 1,
                parent_comment_id=saved_ids.get(parent_idx),
                mentions_brand=1 if mentions else 0,
                status="complete",
                suggested_post_day=comment_day,
                suggested_order=idx,
                prompt_version=PROMPT_VERSION,
                comment_type="hq",
            )
            self._detect_and_store_keywords(comment_id, body, brand, mentions)

            saved_ids[idx] = comment_id
            saved_bodies[idx] = body
            saved_personas[idx] = p_id

            saved.append({
                "id": comment_id, "body": body,
                "is_reply": not is_main, "mentions_brand": mentions,
                "day": comment_day,
                "parent_id": saved_ids.get(parent_idx),
            })

            fp = self._extract_pattern_fingerprint(body, brand["name"], p_id, s_id)
            self._pattern_history.append(fp)

            label = "main comment" if is_main else f"reply {idx} (to #{parent_idx})"
            print(f"    Generated {label}")

        print(f"    HQ thread complete — {len(saved)} comments generated")
        return saved

    # ------------------------------------------------------------------
    # OP Reply generation — post author replies to comments
    # ------------------------------------------------------------------

    def generate_op_replies(self, post, brand, num_replies=3, post_day_offset=0):
        """Generate replies from the OP (post author) to existing comments.

        OP replies never mention brands. They add authenticity by making
        the thread look like a real person posted and is engaging with responses.

        Args:
            post: post dict from DB
            brand: brand dict (for association only, never mentioned)
            num_replies: how many OP replies to generate
            post_day_offset: the day the post is scheduled for

        Returns:
            list of saved comment dicts with IDs
        """
        subreddit = self.db.get_subreddit(post["subreddit_id"])

        # Get existing top-level comments to reply to
        all_comments = self.db.get_comments(post["id"])
        # Only reply to top-level non-OP comments that have actual content
        top_level = [c for c in all_comments
                     if not c["is_reply"]
                     and c.get("comment_type") != "op_reply"
                     and len(c.get("body", "")) > 20]

        if not top_level:
            print("    No top-level comments to reply to")
            return []

        # Select which comments to reply to (random sample, avoid duplicates)
        targets = random.sample(top_level, min(num_replies, len(top_level)))

        all_brand_names = [brand["name"]] if brand else []

        saved = []
        for i, target_comment in enumerate(targets):
            target_body = target_comment["body"]
            target_id = target_comment["id"]

            # Schedule 1-2 days after post (OP checking back)
            reply_day = post_day_offset + random.randint(1, 2)

            prompt = f"""You are the person who wrote this Reddit post. You're replying to a comment on YOUR post.

YOUR POST TITLE: "{post['title']}"
YOUR POST BODY: "{post['body'][:600]}"

SUBREDDIT: r/{subreddit['name']}

COMMENT YOU'RE REPLYING TO:
"{target_body[:500]}"

Write a reply AS THE OP (original poster). You should:
- Sound like the same person who wrote the post
- React naturally to what they said (thank them, ask follow-up, share an update, agree/disagree)
- Reference details from YOUR original post to show consistency
- Be casual and conversational, like a real Reddit OP engaging
- Keep it 1-3 sentences typically (OPs don't write essays in replies)
- Vary your approach: sometimes grateful, sometimes curious, sometimes sharing an update
- NEVER mention any brand name ({', '.join(all_brand_names)}) or any product/company
- NEVER use dashes (-), em-dashes, or double-dashes
- Do NOT start with "Thanks for..." every time, vary your openings

Return JSON only:
{{
    "reply": "your OP reply text"
}}"""

            result = self.claude.call(prompt, max_tokens=500, temperature=0.9)
            if not result or "reply" not in result:
                print(f"    Warning: failed to generate OP reply {i+1}")
                continue

            body = result["reply"]

            # Verify no brand mention
            if brand and brand["name"].lower() in body.lower():
                print(f"    Warning: OP reply mentions brand, skipping")
                continue

            comment_id = self.db.save_comment(
                post_id=post["id"],
                brand_id=brand["id"] if brand else None,
                body=body,
                persona_id="op",
                structure_id="op_reply",
                is_reply=1,
                parent_comment_id=target_id,
                mentions_brand=0,
                status="complete",
                suggested_post_day=reply_day,
                suggested_order=100 + i,  # after regular comments
                prompt_version=PROMPT_VERSION,
                comment_type="op_reply",
            )

            saved.append({
                "id": comment_id, "body": body,
                "is_reply": True, "mentions_brand": False,
                "day": reply_day, "parent_id": target_id,
                "comment_type": "op_reply",
            })
            print(f"    Generated OP reply {i+1} → comment #{target_id}")

        print(f"    OP replies complete — {len(saved)} generated")
        return saved

    def generate_for_existing_post(self, reddit_url, subreddit_id, brand, num_comments,
                                    brand_mention_ratio=None):
        """Generate comments for a post that already has live Reddit comments.

        Fetches live comments, analyzes tone, generates a mix of top-level and replies.
        """
        if brand_mention_ratio is None:
            brand_mention_ratio = DEFAULT_BRAND_MENTION_RATIO

        print(f"    Fetching live comments from Reddit...")
        comments, post_body, is_archived = self.fetch_comments(reddit_url)
        post_title = ""

        # Try to get title from Reddit
        try:
            clean_url = reddit_url.split("?")[0].rstrip("/")
            resp = requests.get(f"{clean_url}.json", headers=self.headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                post_title = data[0]["data"]["children"][0]["data"].get("title", "")
        except Exception:
            pass

        if is_archived:
            print("    Post is archived — cannot comment")
            return []

        subreddit_name = self.extract_subreddit(reddit_url)
        comment_stats = self._compute_comment_stats(comments)

        # Analyze tone
        print(f"    Analyzing tone ({len(comments)} comments)...")
        tone_analysis = self.analyze_tone(post_title, post_body, subreddit_name, comments, comment_stats)

        # Decide brand mention allocation
        num_brand = round(num_comments * brand_mention_ratio)
        if brand_mention_ratio > 0:
            num_brand = max(1, num_brand)  # at least 1 if ratio > 0
        brand_indices = set(random.sample(range(num_comments), min(num_brand, num_comments))) if num_brand > 0 else set()
        if 0 in brand_indices and num_comments > 1:
            brand_indices.discard(0)
            alternatives = [i for i in range(1, num_comments) if i not in brand_indices]
            if alternatives:
                brand_indices.add(random.choice(alternatives))
        mention_flags = [i in brand_indices for i in range(num_comments)]

        # Mix of top-level and replies: ~80% direct, ~20% replies
        num_top = max(1, int(num_comments * 0.8))
        num_reply = num_comments - num_top

        # Pick reply targets
        reply_targets = {}
        if num_reply > 0 and comments:
            for r in range(num_reply):
                target = self._select_reply_target(comments, post_title, brand["name"],
                    {"best_angle": "general", "natural_fit": 2})
                if target:
                    reply_targets[num_top + r] = target

        print(f"    Generating {num_top} top-level + {num_reply} replies...")
        result = self.generate_comments(
            post_title=post_title,
            post_body=post_body,
            subreddit=subreddit_name,
            comments=comments,
            brand_name=brand["name"],
            brand_context=brand["context"],
            num_comments=num_comments,
            tone_analysis=tone_analysis,
            comment_stats=comment_stats,
            mention_brand_flags=mention_flags,
            reply_targets=reply_targets,
            relevance={"best_angle": "general discussion", "natural_fit": 2},
        )

        generated = result.get("generated_comments", [])
        if not generated:
            return []

        # Find or create the post in DB
        post_entry = None
        url_entry = self.db.conn.execute(
            "SELECT post_id FROM post_urls WHERE reddit_url = ?", (reddit_url,)
        ).fetchone()
        if url_entry and url_entry["post_id"]:
            post_entry = self.db.get_post(url_entry["post_id"])

        if not post_entry:
            post_id = self.db.save_post(
                subreddit_id=subreddit_id,
                brand_id=brand["id"],
                title=post_title or "External Reddit Post",
                body=post_body or "",
                storyline="external",
                is_custom=1,
                status="published",
                prompt_version=PROMPT_VERSION,
            )
            self.db.add_post_url(subreddit_id, reddit_url, post_id)
        else:
            post_id = post_entry["id"]

        # Save comments
        saved = []
        personas = result.get("_personas", [])
        structures = result.get("_structures", [])

        for i, body in enumerate(generated):
            is_reply = i >= num_top
            mentions = mention_flags[i] and brand["name"].lower() in body.lower()
            parent_id = None  # For external posts, we don't track parent_comment_id in our DB

            comment_id = self.db.save_comment(
                post_id=post_id,
                brand_id=brand["id"],
                body=body,
                persona_id=personas[i] if i < len(personas) else None,
                structure_id=structures[i] if i < len(structures) else None,
                is_reply=1 if is_reply else 0,
                parent_comment_id=parent_id,
                mentions_brand=1 if mentions else 0,
                status="complete",
                suggested_post_day=0,
                suggested_order=i,
                prompt_version=PROMPT_VERSION,
            )
            self._detect_and_store_keywords(comment_id, body, brand, mentions)
            saved.append({
                "id": comment_id,
                "body": body,
                "is_reply": is_reply,
                "mentions_brand": mentions,
                "reply_to": reply_targets.get(i, {}).get("author", "") if is_reply else "",
            })

            if i < len(personas):
                fp = self._extract_pattern_fingerprint(body, brand["name"], personas[i], structures[i] if i < len(structures) else "unknown")
                self._pattern_history.append(fp)

        return saved
