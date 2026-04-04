"""
Reddit Comment Generator Bot V3 (Improved Quality)
- Auto-extracts brand info from domain
- Context-aware comments that reference the actual post
- Conversation threading (responds to what others said)
- Mandatory brand mention in every comment
- Real user patterns with specifics and honest limitations
- Subreddit-specific language matching
- Two-pass generation: tone analysis → comment writing
- Quality gate with self-evaluation and retry
- Filters archived posts
"""

import anthropic
import requests
import csv
import json
import time
import argparse
import os
import random
from datetime import datetime
from bs4 import BeautifulSoup


# --- Anti-detection: Persona pool ---
# Each persona defines a distinct commenter voice to prevent uniform output.
PERSONAS = [
    {
        "id": "skeptic",
        "voice": "Skeptical and hedging. You doubt most things and only share what worked reluctantly. Use phrases like 'idk if this works for everyone', 'take this with a grain of salt', 'not saying it's the answer but'. You temper any positive statement with doubt.",
        "length": "medium",
    },
    {
        "id": "newbie",
        "voice": "You're relatively new to this and still figuring things out. Ask follow-up questions. Use phrases like 'still learning', 'not sure if this is normal', 'anyone else experience this?'. Show uncertainty and curiosity.",
        "length": "medium",
    },
    {
        "id": "veteran_terse",
        "voice": "You've been around a long time. Terse, no-nonsense, lowercase everything, minimal punctuation. You give blunt short answers. No fluff, no stories. Fragment sentences are fine.",
        "length": "short",
    },
    {
        "id": "data_nerd",
        "voice": "You're data-driven. Mention specific numbers from YOUR OWN experience only: your dosage, your timeline, your costs. Never cite statistics about other people or claim to have tracked/surveyed others. You compare things methodically. Slightly more formal than average.",
        "length": "long",
    },
    {
        "id": "lurker",
        "voice": "You rarely post. You're hesitant but felt compelled to share. Slightly awkward writing style. Never open with cliches like 'usually just lurk' or 'made an account to say'.",
        "length": "medium",
    },
    {
        "id": "frustrated",
        "voice": "You're frustrated with the situation/problem itself, not with specific providers. Nothing feels like a perfect answer yet. Use phrases like 'this whole thing is exhausting', 'still figuring it out', 'nothing works the way you expect'. Zero enthusiasm about anything, including whatever you currently use.",
        "length": "medium-long",
    },
    {
        "id": "helper",
        "voice": "You're primarily here to help the OP. Your entire focus is giving useful advice. Any personal details are secondary. Direct, supportive tone.",
        "length": "medium",
    },
    {
        "id": "comparer",
        "voice": "You've tried 2-3 different options and give honest pros/cons of each, including your current one. Your current choice has clear downsides too. No winner, no favorite. You're still not fully satisfied. Use phrases like 'tried X first', 'then Y', 'currently on Z but it's not perfect either'. Neutral, slightly resigned.",
        "length": "long",
    },
    {
        "id": "tangent",
        "voice": "You start talking about something adjacent to the post topic, then drift between related thoughts. Your comment reads like a stream of consciousness. Use parentheses and asides frequently.",
        "length": "medium-long",
    },
    {
        "id": "agreeable",
        "voice": "You mostly agree with another commenter and add your own twist. Open by referencing what someone else said ('yeah what u/X said', 'this ^', 'seconding this'). You're cosigning someone's take and adding a small personal detail.",
        "length": "short-medium",
    },
    {
        "id": "dry_humor",
        "voice": "Sarcastic and dry. You make wry observations about the situation. Use deadpan humor — no lols or emojis. Phrases like 'ask me how I know', 'fun times', '0/10 do not recommend [the problem]'. Everything is delivered deadpan. No enthusiasm about anything.",
        "length": "short-medium",
    },
    {
        "id": "concerned",
        "voice": "You're worried about doing things right and safety. Ask about side effects, risks, proper monitoring. Phrases like 'make sure you', 'my concern was', 'worth asking your doc about'. Cautious and caring tone. Focused on safety and doing things right.",
        "length": "medium",
    },
    {
        "id": "budget_conscious",
        "voice": "You focus on costs and value. Mention specific prices, compare costs between options. Phrases like 'for the price', 'way cheaper than', 'adds up quick'. Practical, no-frills.",
        "length": "medium",
    },
    {
        "id": "switcher",
        "voice": "You switched recently and honestly not sure it was worth the hassle. The new thing has different problems than the old thing. Grass isn't really greener. Phrases like 'switched but idk', 'old one had X problem, new one has Y problem', 'might switch back honestly'. Mention specific annoyances with your CURRENT choice.",
        "length": "medium-long",
    },
    {
        "id": "long_timer",
        "voice": "You've been doing this for over a year. Very casual about everything — it's just part of your routine now, not exciting or new. Phrases like 'been on this for like a year', 'at this point it's just', 'nothing dramatic to report'. Understated, low-energy.",
        "length": "short-medium",
    },
]

# --- Anti-detection: Comment structure templates ---
# Randomly assigned to each comment to prevent structural uniformity.
STRUCTURE_TEMPLATES = [
    {
        "id": "story_arc",
        "instruction": "Write as a personal narrative: set the scene with your situation, describe what happened, share where you are now.",
    },
    {
        "id": "direct_answer",
        "instruction": "Directly answer the OP's question or address their concern. Give practical advice first. If you used specific tools/providers, they're minor details, not the answer.",
    },
    {
        "id": "reply_to_commenter",
        "instruction": "Respond directly to something a specific commenter said (reference their username or quote them). Build on their point with your own experience.",
    },
    {
        "id": "list_format",
        "instruction": "Structure your comment as a casual list of things that helped you (not numbered/bulleted — just natural flow like 'first thing was X, also Y helped, and Z was the big one'). List them naturally, no item gets special attention.",
    },
    {
        "id": "question_plus_experience",
        "instruction": "Ask the OP a genuine follow-up question about their situation. Then share a brief bit of your own experience that's relevant to the question.",
    },
    {
        "id": "short_punchy",
        "instruction": "Keep it to 2-4 sentences max. One observation about the post, one brief personal take, maybe a closing thought. No storytelling, no journey — just a quick authentic drive-by comment.",
    },
    {
        "id": "comparison",
        "instruction": "Compare 2-3 options you've tried for this. Give honest pros and cons of each. Compare honestly. No favorites. Be balanced.",
    },
    {
        "id": "tangent_drift",
        "instruction": "Start by talking about a related but different aspect of the topic. Your point is about the related topic, everything else is tangential. Use parentheses and natural topic shifts.",
    },
    {
        "id": "devils_advocate",
        "instruction": "Challenge a common piece of advice in the thread, then share your own different experience. Your experience supports your perspective. Shows independent thinking, not agreement.",
    },
    {
        "id": "update_post",
        "instruction": "Frame your comment as if you're sharing an update or following up on something ('so I actually tried what people suggested...', 'update on this since I was in the same boat'). Gives a before/after feel.",
    },
    {
        "id": "anecdote",
        "instruction": "Tell a short story or anecdote only loosely connected to the post. The story should be interesting on its own — funny, surprising, or relatable. Any details about services/tools are incidental.",
    },
    {
        "id": "contrarian",
        "instruction": "Respectfully disagree with the thread consensus or push back on a popular opinion. Offer a different perspective based on your experience. Your experience supports your perspective.",
    },
]


# --- Anti-detection: Banned phrases (marketing fingerprints) ---
BANNED_PHRASES = [
    # Marketing language
    "highly recommend", "definitely recommend", "would recommend", "can't recommend enough",
    "game changer", "life changer", "changed my life",
    "amazing results", "great results", "amazing service", "great service",
    "check them out", "check it out", "look into them", "worth checking out",
    "their support is great", "the process was easy", "the team is great",
    "super easy", "super helpful", "super responsive",
    "ended up going through", "ended up switching to", "ended up using",
    "ended up", "landed on", "handles that part", "was the one that stuck",
    "went through",
    "not gonna lie",
    # Agreement formulas (detectable pattern across comments)
    "makes a good point", "makes a solid point",
    "is spot on", "is absolutely right", "is right about",
    # Filler words (overused — 3-4x natural rate)
    "honestly",
    "actually",
    # Dash patterns (bot punctuation fingerprint)
    " — ", " -- ",
]

# --- Anti-detection: System prompt rotation for generation ---
GENERATION_SYSTEM_PROMPTS = [
    "You are writing a Reddit comment as yourself about a topic you know well. Output valid JSON only — no markdown, no code blocks.",
    "You're a regular person responding in a Reddit thread. Output valid JSON only.",
    "Write as someone participating in this Reddit discussion. Output valid JSON only.",
    "You are a Reddit user responding to a post about a topic you have experience with. Output valid JSON only — no markdown, no explanations.",
    "Respond as a real Reddit user. Be natural and stay on topic. Output valid JSON only.",
]


# --- Anti-detection: Expanded few-shot example pool ---
# 2-3 examples are randomly selected per generation call to prevent template mimicry.
FEW_SHOT_POOL = [
    # Anti-pattern examples only — model learns what NOT to do, has no template to copy.
    # 1. Direct advertisement
    {
        "post": "Anyone else dealing with brain fog lately?",
        "bad": "I've been using BrandX and it really helped with my brain fog. Highly recommend checking them out.",
        "why_bad": "Entire comment is about the brand. No real advice. Reads like an ad.",
    },
    # 2. Brand as the answer
    {
        "post": "What bloodwork should I ask for?",
        "bad": "BrandX does comprehensive bloodwork and their doctors are really knowledgeable about what to test for!",
        "why_bad": "Doesn't answer the question at all. Just redirects to brand. Zero useful info.",
    },
    # 3. Superlative brand praise
    {
        "post": "Best online options? Overwhelmed by choices",
        "bad": "BrandX is the best option out there. Their prices are competitive and the service is amazing.",
        "why_bad": "Every sentence is brand praise. 'Best', 'competitive', 'amazing' are shill words. No comparison, no nuance.",
    },
    # 4. Brand solves everything
    {
        "post": "Is this normal or should I be worried?",
        "bad": "Don't worry, BrandX helped me with the same thing and their doctors are super responsive!",
        "why_bad": "Dismisses concern with brand plug. 'Super responsive' is a marketing phrase. No empathy or real reassurance.",
    },
    # 5. Enthusiastic recommendation
    {
        "post": "My doctor keeps dismissing my concerns",
        "bad": "Switch to BrandX! They actually listen to their patients and the process was super easy.",
        "why_bad": "Exclamation mark enthusiasm. 'Actually listen' and 'super easy' are promotional. Sounds like a sales pitch not a frustrated patient.",
    },
    # 6. Vague positive claim
    {
        "post": "How long before you noticed changes?",
        "bad": "BrandX worked great for me, I noticed results pretty quickly!",
        "why_bad": "No specifics at all. 'Worked great' and 'pretty quickly' are meaningless. Real users give timelines and details.",
    },
    # 7. Brand saves the day
    {
        "post": "Anyone else feel worse before feeling better?",
        "bad": "Yes but BrandX helped me get through it with their great medical support and monitoring!",
        "why_bad": "'Great medical support and monitoring' is straight from a brochure. Real users don't talk like this.",
    },
    # 8. Affordability shill
    {
        "post": "The cost of all this is insane",
        "bad": "BrandX is really affordable compared to other options, definitely check them out!",
        "why_bad": "'Affordable', 'definitely check them out' is an ad. Doesn't engage with the frustration about cost.",
    },
    # 9. Best decision ever
    {
        "post": "Thinking about switching providers",
        "bad": "Switch to BrandX, I made the switch and it was the best decision!",
        "why_bad": "'Best decision' is a review-site cliche. Imperative 'Switch to BrandX' is a direct sales push.",
    },
    # 10. Highly recommend
    {
        "post": "Is telehealth legit for this?",
        "bad": "Yes! BrandX is an amazing telehealth service that I highly recommend!",
        "why_bad": "'Amazing', 'highly recommend', double exclamation. Every bot flag in one sentence.",
    },
    # 11. Brand continuation pattern
    {
        "post": "Local clinic vs online - what's better?",
        "bad": "I use BrandX and they have great doctors who monitor everything and adjust your protocol based on labs and they're really responsive to messages too.",
        "why_bad": "Brand name followed by multiple clauses describing what they do. 'and they... and they...' continuation is the #1 bot tell. Brand should be mentioned and immediately dropped.",
    },
    # 12. Brand as entire identity
    {
        "post": "Just got my first set of results back and I'm confused",
        "bad": "BrandX doctors helped me understand my results, they're really knowledgeable!",
        "why_bad": "Brand is the subject and hero of every sentence. Real users talk about their experience, not the company.",
    },
    # 13. Brand makes it easy
    {
        "post": "How do you explain this to your partner?",
        "bad": "BrandX makes the process really easy and discreet, my partner barely noticed!",
        "why_bad": "'Makes the process easy and discreet' is feature marketing. Comment doesn't share any real personal experience.",
    },
    # 14. Worth every penny
    {
        "post": "Is it worth the money?",
        "bad": "BrandX is really affordable and worth every penny!",
        "why_bad": "'Worth every penny' is a cliche review phrase. No actual cost numbers, no tradeoffs, no real thought.",
    },
    # 15. Brand as better alternative
    {
        "post": "Everyone says X is the best approach",
        "bad": "Actually BrandX offers a better approach that I'd recommend!",
        "why_bad": "'Actually BrandX offers' positions brand as the superior option. Real disagreement is about the approach, not about pushing a provider.",
    },
    # 16. Problem→brand-solution arc (subtle marketing structure)
    {
        "post": "My doctor keeps dismissing my symptoms",
        "bad": "My old doctor was the same way, totally useless. Switched to BrandX and they actually ran the right tests. Night and day difference.",
        "why_bad": "Classic marketing testimonial structure: [negative experience with alternative] → [brand as solution] → [positive outcome]. The brand is positioned as the hero that solved the problem. Real users don't structure stories this way.",
    },
    # 17. Category marketing (brand's type framed as superior)
    {
        "post": "Should I go with a local option or online?",
        "bad": "Online is way more convenient and saves so much time. I use BrandX and never have to deal with scheduling hassles or long waits anymore.",
        "why_bad": "Frames the brand's entire category as superior, then names the brand as the specific choice. This is two layers of marketing: category positioning + brand positioning. Real users mention tradeoffs for both.",
    },
]


def _select_few_shot_examples(n=3):
    """Randomly select n anti-pattern examples from the pool."""
    selected = random.sample(FEW_SHOT_POOL, min(n, len(FEW_SHOT_POOL)))
    lines = ["EXAMPLES OF WHAT NOT TO DO (avoid these patterns completely):"]
    for i, ex in enumerate(selected, 1):
        lines.append(f"\n--- Anti-Pattern {i} ---")
        lines.append(f'POST: "{ex["post"]}"')
        lines.append(f'BAD COMMENT: "{ex["bad"]}"')
        lines.append(f"WHY IT'S BAD: {ex['why_bad']}")
    return "\n".join(lines)


class CommentGeneratorBot:
    def __init__(self, anthropic_api_key):
        """Initialize the bot with Anthropic API key."""
        self.client = anthropic.Anthropic(api_key=anthropic_api_key)
        self.pullpush_url = "https://api.pullpush.io/reddit/search/comment"
        self.reddit_base = "https://www.reddit.com"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        # Anti-detection: track patterns across the batch to avoid repetition
        self._pattern_history = []

    def _extract_pattern_fingerprint(self, comment, brand_name, persona_id, structure_id):
        """Extract a fingerprint from a generated comment for cross-post pattern tracking."""
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
        """Build a prompt section telling the model what patterns to avoid based on history.
        Designed to scale to 100+ comments: summarizes global counts + least-used suggestions,
        caps output to ~200 words to avoid prompt bloat.
        """
        if not self._pattern_history:
            return ""

        from collections import Counter

        all_history = self._pattern_history
        recent = all_history[-12:]  # recent window for openings/intros

        # Global counts across full batch
        global_personas = Counter(p["persona"] for p in all_history)
        global_structures = Counter(p["structure"] for p in all_history)

        # Recent openings (don't need full history, just last 12)
        recent_openings = Counter(p["first_five"] for p in recent)
        lines = ["\nVARIETY GUIDANCE (avoid repeating patterns from this batch):"]

        # Recent openings to avoid
        top_openings = [o for o, _ in recent_openings.most_common(6)]
        if top_openings:
            lines.append(f"  Recent openings (avoid): {', '.join(repr(o) for o in top_openings)}")

        # Global persona/structure usage — show least used to encourage variety
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

    def _select_comment_config(self, tone_analysis, comment_stats, relevance, num_comments):
        """Use tone/relevance analysis to pick best-fitting personas, structures, and brand intros.

        Returns: (selected_personas, selected_structures, per_comment_angles)
        Uses weighted random selection so fitting options are likely but variety is preserved.
        """
        from collections import Counter

        # --- Score each persona for fit based on tone analysis ---
        tone = tone_analysis or {}
        emotional = (tone.get("emotional_tone", "") + " " + tone.get("overall_vibe", "")).lower()
        formality = tone.get("formality", "").lower()
        technical = tone.get("technical_level", "").lower()
        avg_words = (comment_stats or {}).get("avg_words", 50)

        # Persona fit scoring: base weight 1, bonus +2 for good fit
        persona_weights = []
        for p in PERSONAS:
            w = 1.0  # base weight — every persona has a chance
            pid = p["id"]
            # Emotional tone matching
            if any(k in emotional for k in ["supportive", "helpful", "encouraging"]):
                if pid in ("helper", "lurker", "concerned"):
                    w += 2.0
            if any(k in emotional for k in ["skeptical", "cynical", "frustrated", "hostile"]):
                if pid in ("skeptic", "frustrated", "dry_humor", "contrarian"):
                    w += 2.0
            # Technical level matching
            if any(k in technical for k in ["high", "technical", "detailed", "data"]):
                if pid in ("data_nerd", "veteran_terse", "comparer"):
                    w += 2.0
            # Formality matching
            if any(k in formality for k in ["very informal", "casual", "slang"]):
                if pid in ("veteran_terse", "tangent", "newbie", "dry_humor"):
                    w += 1.5
            if any(k in formality for k in ["semi-formal", "professional", "formal"]):
                if pid in ("helper", "data_nerd", "comparer", "concerned"):
                    w += 1.5
            # Length matching
            if avg_words < 30:
                if p["length"] in ("short", "short-medium"):
                    w += 1.0
            elif avg_words > 70:
                if p["length"] in ("long", "medium-long"):
                    w += 1.0
            persona_weights.append(w)

        # --- Score each structure for fit based on stats + relevance ---
        best_angle = (relevance or {}).get("best_angle", "").lower()
        natural_fit = (relevance or {}).get("natural_fit", 1)

        structure_weights = []
        for s in STRUCTURE_TEMPLATES:
            w = 1.0
            sid = s["id"]
            # Length-based structure preference
            if avg_words < 30:
                if sid in ("short_punchy", "direct_answer"):
                    w += 2.0
                if sid in ("story_arc", "comparison", "anecdote"):
                    w -= 0.5  # penalize long structures in short threads
            elif avg_words > 70:
                if sid in ("story_arc", "comparison", "tangent_drift", "anecdote"):
                    w += 2.0
                if sid in ("short_punchy",):
                    w -= 0.5
            # Angle-based preference
            if any(k in best_angle for k in ["question", "asking", "advice"]):
                if sid in ("direct_answer", "question_plus_experience", "helper"):
                    w += 1.5
            if any(k in best_angle for k in ["compar", "alternative", "option", "switch"]):
                if sid in ("comparison", "list_format"):
                    w += 1.5
            if any(k in best_angle for k in ["experience", "story", "journey"]):
                if sid in ("story_arc", "anecdote", "update_post"):
                    w += 1.5
            # Ensure weight is at least 0.3
            w = max(w, 0.3)
            structure_weights.append(w)

        # --- Deduplicate against recent history ---
        recent_personas = set()
        recent_structures = set()
        if self._pattern_history:
            lookback = self._pattern_history[-8:]
            recent_personas = {p["persona"] for p in lookback}
            recent_structures = {p["structure"] for p in lookback}

        # Penalize recently used options (halve their weight)
        for i, p in enumerate(PERSONAS):
            if p["id"] in recent_personas:
                persona_weights[i] *= 0.3
        for i, s in enumerate(STRUCTURE_TEMPLATES):
            if s["id"] in recent_structures:
                structure_weights[i] *= 0.3

        # --- Weighted selection (no duplicates within this call) ---
        selected_personas = []
        remaining_p_weights = list(persona_weights)
        remaining_p_indices = list(range(len(PERSONAS)))
        for _ in range(num_comments):
            if not remaining_p_indices:
                break
            chosen = random.choices(remaining_p_indices, weights=[remaining_p_weights[i] for i in remaining_p_indices], k=1)[0]
            selected_personas.append(PERSONAS[chosen])
            remaining_p_indices.remove(chosen)

        selected_structures = []
        remaining_s_weights = list(structure_weights)
        remaining_s_indices = list(range(len(STRUCTURE_TEMPLATES)))
        for _ in range(num_comments):
            if not remaining_s_indices:
                break
            chosen = random.choices(remaining_s_indices, weights=[remaining_s_weights[i] for i in remaining_s_indices], k=1)[0]
            selected_structures.append(STRUCTURE_TEMPLATES[chosen])
            remaining_s_indices.remove(chosen)

        # --- Per-comment angle hints ---
        per_comment_angles = []
        base_angle = (relevance or {}).get("best_angle", "")
        if num_comments >= 2:
            per_comment_angles.append(f"Focus on the OP's post: {base_angle}" if base_angle else "Respond to the OP's main question/concern")
            # Second comment: analysis-driven angle based on tone/relevance
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
            if num_comments >= 3:
                per_comment_angles.append(
                    "REPLY MODE: This is a direct reply to a specific comment in the thread, "
                    "not a top-level comment. Respond naturally to what THEY said. Do NOT address the OP."
                )
            for _ in range(num_comments - min(num_comments, 3)):
                per_comment_angles.append(base_angle or "Find a unique angle into this conversation")
        else:
            per_comment_angles = [base_angle or "Respond to the OP's main question/concern"]

        return selected_personas, selected_structures, per_comment_angles

    def extract_brand_info(self, domain):
        """Fetch a domain's homepage and use Claude to extract brand name, context, and keywords."""
        # Normalize URL
        url = domain if domain.startswith("http") else f"https://{domain}"

        print(f"    🌐 Fetching {url}...")
        try:
            response = requests.get(url, headers=self.headers, timeout=15, allow_redirects=True)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"    ❌ Failed to fetch domain: {e}")
            return None

        # Parse HTML
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        meta_desc = ""
        meta_tag = soup.find("meta", attrs={"name": "description"})
        if not meta_tag:
            meta_tag = soup.find("meta", attrs={"property": "og:description"})
        if meta_tag:
            meta_desc = meta_tag.get("content", "")

        headings = [h.get_text(strip=True) for h in soup.find_all(["h1", "h2"])[:8]]
        paragraphs = [p.get_text(strip=True) for p in soup.find_all("p")[:5] if len(p.get_text(strip=True)) > 20]
        first_paragraphs = " ".join(paragraphs)[:800]

        if not title and not meta_desc and not headings:
            print(f"    ⚠ Page has minimal content (may use JavaScript rendering)")
            return None

        print(f"    🔍 Analyzing brand info...")
        prompt = f"""Analyze this website's homepage content and extract brand information.

DOMAIN: {domain}
PAGE TITLE: {title}
META DESCRIPTION: {meta_desc}
HEADINGS: {', '.join(headings)}
PAGE CONTENT: {first_paragraphs}

Determine:
1. The brand/company name (just the name, not taglines)
2. A concise description of what the brand does (1-2 sentences, written as if explaining to someone — e.g. "Men's telehealth clinic specializing in TRT and weight loss programs")
3. 5-10 relevant keywords someone searching for this type of product/service would use

Return JSON only:
{{
    "brand_name": "the brand name",
    "brand_context": "what the brand does",
    "brand_keywords": ["keyword1", "keyword2", "keyword3"]
}}"""

        result = self._call_claude(prompt, max_tokens=512, temperature=0.3)
        if result and result.get("brand_name") and result.get("brand_context"):
            return result

        print(f"    ⚠ Could not extract brand info from page content")
        return None

    def extract_post_id(self, url):
        """Extract post ID from Reddit URL."""
        try:
            parts = url.split("/comments/")
            if len(parts) > 1:
                return parts[1].split("/")[0]
        except (AttributeError, IndexError):
            pass
        return None

    def extract_subreddit(self, url):
        """Extract subreddit from Reddit URL."""
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
            print(f"    ⚠ Could not extract post ID from URL")
            return [], "", False

        comments = []
        post_body = ""
        is_archived = False

        # Try Reddit's native JSON API
        for attempt in range(max_retries):
            try:
                clean_url = post_url.split("?")[0].rstrip("/")
                json_url = f"{clean_url}.json"

                response = requests.get(json_url, headers=self.headers, timeout=30)

                if response.status_code == 429:
                    wait = min(2 ** attempt * 3, 30)
                    print(f"    ⚠ Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                data = response.json()

                # Get post data
                if len(data) > 0:
                    post_data = data[0].get("data", {}).get("children", [{}])[0].get("data", {})
                    post_body = post_data.get("selftext", "")[:1000]
                    is_archived = post_data.get("archived", False)

                # Get comments
                if len(data) > 1:
                    comment_data = data[1].get("data", {}).get("children", [])

                    for comment in comment_data[:limit]:
                        if comment.get("kind") != "t1":
                            continue

                        c = comment.get("data", {})
                        body = c.get("body", "")

                        if body in ["[deleted]", "[removed]", ""] or len(body) < 10:
                            continue

                        comments.append({
                            "body": body[:600],
                            "score": c.get("score", 0),
                            "author": c.get("author", "unknown"),
                            "id": c.get("id", ""),
                            "permalink": c.get("permalink", ""),
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
            params = {
                "link_id": post_id,
                "size": limit,
                "sort": "desc",
                "sort_type": "score"
            }

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
                })

        except requests.exceptions.RequestException as e:
            print(f"    Pullpush failed: {str(e)[:50]}")

        return comments, post_body, is_archived

    def _compute_comment_stats(self, comments):
        """Compute average length statistics from fetched comments."""
        if not comments:
            return {"avg_chars": 200, "avg_words": 40, "median_chars": 200, "min_chars": 50, "max_chars": 500}

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
        }

    def _select_reply_target(self, comments, post_title, brand_name, relevance):
        """Pick the best existing comment to reply to. Uses heuristic scoring (no API call)."""
        if not comments:
            return None

        best_angle = (relevance or {}).get("best_angle", "").lower()
        brand_lower = brand_name.lower()

        # Extract keywords from best_angle for relevance matching
        angle_words = set(best_angle.split()) - {"the", "a", "an", "is", "are", "to", "for", "and", "or", "with", "in", "on", "of"}

        scored = []
        for c in comments:
            # Skip bots, deleted users, very short comments
            if c["author"].lower() in ("automoderator", "[deleted]", "unknown", "bot"):
                continue
            if len(c["body"]) < 20:
                continue
            # Skip comments that already mention the brand
            if brand_lower in c["body"].lower():
                continue

            score = 0.0
            # Popularity (log scale to avoid outliers dominating)
            comment_score = max(c.get("score", 1), 1)
            score += min(comment_score, 50)  # cap at 50

            # Relevance: keyword overlap with best_angle
            body_lower = c["body"].lower()
            overlap = sum(1 for w in angle_words if w in body_lower)
            score += overlap * 5

            # Bonus for questions (great reply targets)
            if "?" in c["body"]:
                score += 10

            # Bonus for medium-length comments (enough substance to reply to)
            word_count = len(c["body"].split())
            if 20 <= word_count <= 100:
                score += 5

            scored.append((score, c))

        if not scored:
            # Fallback: just pick the highest-score comment that isn't AutoMod
            fallback = [c for c in comments if c["author"].lower() != "automoderator" and len(c["body"]) >= 20]
            return fallback[0] if fallback else comments[0]

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    def check_relevance(self, post_title, post_body, subreddit, comments, brand_name, brand_context, brand_keywords=None):
        """Check if the post is relevant for brand mention. Returns raw scores — threshold applied by caller."""

        if not comments:
            return {
                "score": 0,
                "disqualified": False,
                "reason": "No comments to analyze"
            }

        comments_text = "\n".join([
            f'- "{c["body"][:250]}"'
            for c in comments[:10]
        ])

        keywords_text = f"\nBRAND KEYWORDS: {', '.join(brand_keywords)}" if brand_keywords else ""
        post_body_text = f"\nPOST BODY: \"{post_body[:500]}\"" if post_body else ""

        prompt = f"""Analyze if this Reddit post is relevant for naturally mentioning a brand.

POST TITLE: "{post_title}"
SUBREDDIT: r/{subreddit}{post_body_text}

TOP COMMENTS:
{comments_text}

BRAND: {brand_name}
WHAT BRAND DOES: {brand_context}{keywords_text}

Score 0-10 on these criteria:

1. TOPIC MATCH (0-3): Is the post about something the brand offers/solves?
   3 = Direct match (post is about exactly what brand does)
   2 = Related topic (brand could naturally fit)
   1 = Tangentially related
   0 = Completely unrelated

2. PROBLEM-SOLUTION FIT (0-3): Is someone describing a problem the brand solves?
   3 = Explicit problem brand solves ("where can I find...", "struggling with...")
   2 = Implicit problem (describing symptoms/issues brand addresses)
   1 = General discussion
   0 = No problem or brand can't help

3. NATURAL FIT (0-2): Would a brand mention feel organic?
   2 = People are asking for recommendations or sharing solutions
   1 = Experience sharing is happening (can add yours)
   0 = Would feel forced, off-topic, or spammy

4. CONVERSATION OPENING (0-2): Is there a natural way to enter this conversation?
   2 = Clear opening (question asked, advice sought, experiences shared)
   1 = Possible but requires care
   0 = Closed discussion, argument, or meme thread

DISQUALIFIERS (auto-fail):
- Meme/joke post
- Hostile to brands/advertising
- Brand already mentioned
- Completely off-topic

Return JSON only:
{{
    "topic_match": 0-3,
    "problem_fit": 0-3,
    "natural_fit": 0-2,
    "conversation_opening": 0-2,
    "total_score": 0-10,
    "disqualified": true/false,
    "disqualify_reason": "",
    "recommendation": "GENERATE" or "SKIP",
    "best_angle": "Brief description of how brand could naturally fit (if relevant)",
    "summary": "One sentence explanation"
}}"""

        result = self._call_claude(prompt, temperature=0.3)

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
        """Analyze the tone, style, and patterns of existing comments. Returns structured tone data."""

        if not comments:
            return None

        comments_text = "\n".join([
            f'{i+1}. [Score: {c["score"]}] u/{c["author"]}: "{c["body"]}"'
            for i, c in enumerate(comments[:12])
        ])

        post_body_text = f'\nPOST BODY: """{post_body[:500]}"""' if post_body else ""

        prompt = f"""You are a sociolinguistic analyst. Study these Reddit comments and produce a detailed style guide for writing comments that would blend in perfectly.

POST TITLE: "{post_title}"
SUBREDDIT: r/{subreddit}{post_body_text}

EXISTING COMMENTS (with scores):
{comments_text}

MEASURED COMMENT STATISTICS:
- Average: {comment_stats['avg_words']} words / {comment_stats['avg_chars']} characters
- Median: {comment_stats['median_chars']} characters
- Range: {comment_stats['min_chars']}-{comment_stats['max_chars']} characters

Analyze these aspects:

1. FORMALITY: How formal/informal? Lowercase starts? Contractions? Slang?
2. HUMOR STYLE: What kind of humor, if any? Sarcasm, self-deprecating, memes?
3. TECHNICAL LEVEL: How much domain knowledge is assumed?
4. COMMON PHRASES: List 5-10 actual phrases/slang you see used.
5. COMMENT LENGTH: Based on the measured stats, what word count range fits naturally?
6. OVERALL VIBE: What's the emotional undercurrent?
7. SENTENCE STRUCTURE: Long sentences? Fragments? Bullet points?
8. CAPITALIZATION: All lowercase? Normal? ALL CAPS for emphasis?
9. PUNCTUATION: Heavy punctuation or minimal? Dashes vs periods?
10. EMOTIONAL TONE: Supportive? Hostile? Neutral? Skeptical?

Return JSON only:
{{
    "formality": "description of formality level",
    "humor_style": "type of humor if any",
    "technical_level": "how technical the discussion is",
    "common_phrases": ["phrase1", "phrase2", "phrase3"],
    "avg_length_words": {comment_stats['avg_words']},
    "target_word_count_range": "X-Y words",
    "overall_vibe": "description of emotional undercurrent",
    "sentence_structure": "how sentences are structured",
    "capitalization": "capitalization patterns",
    "punctuation_style": "punctuation patterns",
    "emotional_tone": "emotional tone description"
}}"""

        result = self._call_claude(prompt, max_tokens=512, temperature=0.3)
        return result

    def generate_comments(self, post_title, post_body, subreddit, comments, brand_name, brand_context, best_angle="", num_comments=2, tone_analysis=None, comment_stats=None, retry_feedback=None, relevance=None, reply_targets=None):
        """Generate contextual, natural comments with analysis-driven anti-detection."""

        if not comments:
            return {"generated_comments": [], "strategies_used": [], "_personas": [], "_structures": []}

        # Format comments with more context
        comments_text = "\n".join([
            f'{i+1}. [Score: {c["score"]}] u/{c["author"]}: "{c["body"]}"'
            for i, c in enumerate(comments[:15])
        ])

        post_body_text = f'\nPOST BODY: """{post_body[:800]}"""' if post_body else ""

        # --- Analysis-driven selection: persona, structure, angles ---
        selected_personas, selected_structures, per_comment_angles = \
            self._select_comment_config(tone_analysis, comment_stats, relevance, num_comments)

        # --- Anti-detection: Select random few-shot examples ---
        few_shot_text = _select_few_shot_examples(n=3)

        # Build per-comment instructions with unique angles
        reply_targets = reply_targets or {}
        comment_instructions = []
        for idx in range(num_comments):
            persona = selected_personas[idx] if idx < len(selected_personas) else random.choice(PERSONAS)
            structure = selected_structures[idx] if idx < len(selected_structures) else random.choice(STRUCTURE_TEMPLATES)
            angle = per_comment_angles[idx] if idx < len(per_comment_angles) else ""
            angle_line = f"\n    ANGLE: {angle}" if angle else ""

            # For reply comments, add the target comment context
            reply_line = ""
            if idx in reply_targets:
                target = reply_targets[idx]
                reply_line = (
                    f"\n    TARGET COMMENT by u/{target['author']}: \"{target['body'][:400]}\""
                    f"\n    Write as if you clicked 'reply' on their comment. Respond to what THEY said specifically. Do NOT address the OP directly."
                )

            comment_instructions.append(
                f"  Comment {idx+1}:\n"
                f"    PERSONA: {persona['voice']}\n"
                f"    STRUCTURE: {structure['instruction']}\n"
                f"    LENGTH: {persona['length']}{angle_line}{reply_line}"
            )
        per_comment_section = "\n".join(comment_instructions)

        # Build tone section from pre-computed analysis or fallback
        if tone_analysis:
            tone_section = f"""
TONE ANALYSIS (match this style precisely):
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
            tone_section = "\nTONE: Match the exact style of the existing comments above — formality, slang, sentence structure, caps, punctuation."

        # Build length constraint from measured stats
        if comment_stats and comment_stats.get("avg_words", 0) > 0:
            min_words = max(10, int(comment_stats["avg_words"] * 0.5))
            max_words = int(comment_stats["avg_words"] * 1.6)
            length_section = f"\nLENGTH: Average comment here is {comment_stats['avg_words']} words. Aim for {min_words}-{max_words} words but vary widely. Some comments should be much shorter, some longer."
        else:
            length_section = ""

        # Build retry feedback section
        retry_section = ""
        if retry_feedback:
            retry_section = f"""
PREVIOUS ATTEMPT FAILED — fix these issues:
{retry_feedback}
Do NOT repeat the same mistakes."""

        # Build pattern avoidance section from history
        pattern_avoidance = self._build_pattern_avoidance_section()

        # Build banned phrases reminder
        banned_sample = random.sample(BANNED_PHRASES, min(10, len(BANNED_PHRASES)))
        banned_text = ", ".join(f'"{p}"' for p in banned_sample)

        prompt = f"""You're commenting in a Reddit thread about a topic you know well. You happen to use {brand_name} but that's not what your comment is about — your comment is about the TOPIC itself.

POST: "{post_title}"
SUBREDDIT: r/{subreddit}{post_body_text}

EXISTING COMMENTS:
{comments_text}
{tone_section}
{length_section}

STYLE PRIORITY: The EXISTING COMMENTS above are your primary style guide. Match their vocabulary, length, formality, and energy exactly. The anti-pattern examples at the bottom show what NOT to do.

EACH COMMENT HAS A UNIQUE ASSIGNMENT — follow these carefully:
{per_comment_section}

BRAND RULES (follow these EXACTLY):

RULE #1 — MOST IMPORTANT: After you write "{brand_name}", IMMEDIATELY change topic. End the clause or start a new sentence about something else.
Do NOT continue talking about {brand_name}. Do NOT use "they", "them", "their", "it" to refer back to the brand. Do NOT describe what they do.

BAD (continues talking about brand after mention):
  "{brand_name} and they actually monitor everything properly"
  "{brand_name} since they look at the full picture"
  "{brand_name} who suggested daily micro-dosing"
  "{brand_name} and doc there walked me through the math"
  "{brand_name} and they actually discussed hair loss prevention"

Other brand rules:
- Mention {brand_name} exactly once
- For 3+ sentence comments: brand NEVER in first two sentences or last sentence
- For 1-2 sentence comments: brand mid-sentence in parenthetical only
- When mentioning {brand_name}, be neutral and brief. Just name-drop and move on.

ANTI-MARKETING RULES (CRITICAL):
- Do NOT set up a negative experience (with other providers, products, approaches, the old way of doing things) and then introduce {brand_name} as the solution or next step. If you mention frustration with alternatives, {brand_name} must NOT appear nearby or as the resolution.
- If you mention ANY frustration with alternatives, you MUST also mention at least one specific downside or annoyance about {brand_name}. Real users complain about everything, even things they use.
- Do NOT position {brand_name}'s category (online vs local, app vs manual, new vs old) as obviously better. If you compare categories, be balanced or mention downsides.
- The brand mention must be in a COMPLETELY SEPARATE thought from any criticism of alternatives.

ALSO BAD (problem-then-brand-solution arc):
  "my old doc was useless... switched to {brand_name}"
  "got sick of waiting weeks for appointments. {brand_name} handles that"
  "after trying three bad options, {brand_name} was the one that worked"

NEVER USE THESE PHRASES: {banned_text}
{pattern_avoidance}

COMMENT QUALITY RULES:
- Reference something specific from THIS post (title, body, or another commenter)
- Each comment must be structurally different from the other
- Your comment MUST be valuable if the brand clause were deleted
- NEVER describe what {brand_name} does or how it works
- Do NOT open two comments the same way
- Your opening sentence must be completely unique, not a stock phrase
- Limit filler words (just, like, honestly, actually, basically) to maximum ONE per comment
- Do NOT use dashes or emdashes to connect thoughts. Use periods or commas instead.
- Vary punctuation naturally. Occasional ! or ; is fine.
- Match your medical/technical knowledge to your PERSONA. Newbies and lurkers don't know jargon like SHBG, E2, protocol, free T. Only expert personas should use technical terms.
- Write like a casual Reddit user. Don't be too articulate or structured. Real users ramble, leave thoughts incomplete, and don't always make perfect points.
{retry_section}

{few_shot_text}

You MUST generate exactly {num_comments} comments. Return JSON only:
{{
    "generated_comments": [
        {', '.join(f'"comment {i+1}"' for i in range(num_comments))}
    ],
    "strategies_used": [{', '.join(f'"strategy for comment {i+1}"' for i in range(num_comments))}]
}}"""

        temperature = 0.95 if retry_feedback else 0.9
        system_prompt = random.choice(GENERATION_SYSTEM_PROMPTS)
        max_tok = 2000 if num_comments <= 2 else 3000
        result = self._call_claude(prompt, max_tokens=max_tok, temperature=temperature, system_prompt=system_prompt)

        if not result:
            return {"generated_comments": [], "strategies_used": [], "_personas": [], "_structures": []}

        # Attach metadata for pattern tracking
        result["_personas"] = [p["id"] for p in selected_personas[:num_comments]]
        result["_structures"] = [s["id"] for s in selected_structures[:num_comments]]

        return result

    def validate_comments(self, post_title, post_body, subreddit, comments, brand_name, generated_comments, tone_analysis=None):
        """Score generated comments on authenticity and quality. Returns scores and feedback."""

        if not generated_comments:
            return {"evaluations": [], "any_failed": True}

        # Format real comments for reference
        real_comments_text = "\n".join([
            f'- u/{c["author"]}: "{c["body"][:200]}"'
            for c in comments[:5]
        ])

        # Format generated comments
        gen_text = "\n".join([
            f'Comment {i+1}: """{comment}"""'
            for i, comment in enumerate(generated_comments)
        ])

        tone_section = ""
        if tone_analysis:
            tone_section = f"""
EXPECTED TONE:
  Formality: {tone_analysis.get('formality', '?')}
  Vibe: {tone_analysis.get('overall_vibe', '?')}
  Target length: {tone_analysis.get('target_word_count_range', '?')}"""

        prompt = f"""You are a Reddit authenticity evaluator. Determine if these generated comments would pass as genuine user contributions or get flagged as astroturfing/shilling.

ORIGINAL POST:
Title: "{post_title}"
Subreddit: r/{subreddit}
Body: {(post_body or '')[:500]}

REAL COMMENTS FROM THIS THREAD (for tone reference):
{real_comments_text}
{tone_section}

BRAND BEING MENTIONED: {brand_name}

GENERATED COMMENTS TO EVALUATE:
{gen_text}

Score each generated comment on these criteria:

1. AUTHENTICITY (1-10): Would a real Reddit user write this?
   Watch for: too-perfect structure, missing personal detail, reads like it was written to promote something

2. POST RELEVANCE (1-10): Does it engage with THIS specific post?
   References specific details from title/body? Responds to other comments? Could NOT be copy-pasted to a different thread?

3. TONE MATCH (1-10): Does it match the subreddit culture?
   Formality level matches? Vocabulary/slang appropriate? Comment length in right range?

4. MARKETING LANGUAGE CHECK (true/false):
   Does the comment contain ANY of these phrases or patterns?
   - "highly recommend" / "definitely recommend" / "would recommend"
   - "game changer" / "life changer"
   - "amazing results" / "great results" / "amazing service"
   - "check them out" / "look into them" / "worth checking out"
   - "their support is great" / "the process was easy" / "the team is great"
   Mark as true if ANY of these are present.

5. STRUCTURAL PROMOTION CHECK (true/false):
   Does the comment's STRUCTURE read like a brand testimonial, even without banned phrases?
   Check for ALL of these patterns:
   - problem → brand → result arc (classic testimonial structure)
   - Comment describes what the brand DOES, its features, or HOW it works
   - Brand gets more than one clause or sentence of attention
   - Comment would make sense as a customer review on the brand's website
   - The brand is the most memorable part of the comment
   Mark as true if ANY of these structural patterns are present.

PASS CONDITION:
- A comment passes if: average of scores 1-3 >= 7 AND marketing_language is false AND structural_promotion is false
- Marketing phrases OR structural promotion = immediate fail regardless of other scores

Be strict — false passes are worse than false failures.

Return JSON only:
{{
    "evaluations": [
        {{
            "comment_index": 0,
            "authenticity_score": 1-10,
            "post_relevance_score": 1-10,
            "tone_match_score": 1-10,
            "marketing_language": true/false,
            "marketing_phrases_found": ["exact phrase if any"],
            "structural_promotion": true/false,
            "structural_promotion_reason": "why it reads as promotional, if applicable",
            "overall_score": "average of authenticity + post_relevance + tone_match",
            "pass": "true if overall >= 7 AND marketing_language is false AND structural_promotion is false",
            "feedback": "specific actionable fix if failed; empty string if passed"
        }}
    ],
    "any_failed": true/false
}}"""

        result = self._call_claude(prompt, max_tokens=1024, temperature=0.2)

        if not result:
            return {"evaluations": [], "any_failed": False}  # On error, don't block

        # Programmatic marketing phrase check — override pass if phrases detected in comment text
        evals = result.get("evaluations", [])
        for ev_idx, ev in enumerate(evals):
            if ev_idx < len(generated_comments):
                comment_lower = generated_comments[ev_idx].lower()
                found = [p for p in BANNED_PHRASES if p in comment_lower]
                if found:
                    ev["marketing_language"] = True
                    ev["marketing_phrases_found"] = found
                    ev["pass"] = False
                    existing_fb = ev.get("feedback", "")
                    ev["feedback"] = (
                        f"Marketing phrases detected: {found}. Rewrite to remove these — "
                        "replace with a personal, specific observation instead. "
                        + existing_fb
                    ).strip()

        # Programmatic brand-weight check — fail if brand occupies too many sentences
        brand_lower = brand_name.lower()
        for ev_idx, ev in enumerate(evals):
            if ev_idx < len(generated_comments):
                comment_lower = generated_comments[ev_idx].lower()
                if brand_lower in comment_lower:
                    sentences = comment_lower.replace('!', '.').replace('?', '.').split('.')
                    sentences = [s.strip() for s in sentences if s.strip()]
                    brand_sentences = sum(1 for s in sentences if brand_lower in s)
                    total_sentences = len(sentences)
                    if total_sentences > 0 and brand_sentences / total_sentences > 0.3:
                        ev["pass"] = False
                        ev["structural_promotion"] = True
                        existing_fb = ev.get("feedback", "")
                        ev["feedback"] = (
                            f"Brand occupies {brand_sentences}/{total_sentences} sentences "
                            f"({int(brand_sentences / total_sentences * 100)}%). "
                            "Reduce to 1 sentence max. " + existing_fb
                        ).strip()

        # Programmatic brand-continuation check — fail if brand is followed by
        # continuation clauses like "and they...", "since they...", "who ..."
        _continuation_patterns = [
            f"{brand_lower} and they", f"{brand_lower} and their",
            f"{brand_lower} since they", f"{brand_lower} because they",
            f"{brand_lower} who ", f"{brand_lower} where they",
            f"{brand_lower} which ", f"{brand_lower} that ",
            f"{brand_lower} and it", f"{brand_lower} and the ",
            f"{brand_lower}, they", f"{brand_lower}, which",
            f"{brand_lower}, and they",
        ]
        for ev_idx, ev in enumerate(evals):
            if ev_idx < len(generated_comments):
                comment_lower = generated_comments[ev_idx].lower()
                if brand_lower in comment_lower:
                    for pat in _continuation_patterns:
                        if pat in comment_lower:
                            ev["pass"] = False
                            existing_fb = ev.get("feedback", "")
                            ev["feedback"] = (
                                f"Brand continuation detected ('{pat}'). "
                                "After mentioning the brand, immediately move on "
                                "to a different thought. Do NOT describe what the "
                                "brand does or continue talking about it. "
                                + existing_fb
                            ).strip()
                            break

        # Programmatic problem→brand-solution arc check
        # Detect negative sentiment words in the sentence(s) immediately before brand mention
        _negative_precursors = [
            "useless", "terrible", "awful", "horrible", "worst", "sucked",
            "dismissing", "dismissed", "ignored", "brushed off", "wouldn't listen",
            "waste of time", "waste of money", "rip off", "scam",
            "sick of", "fed up", "frustrated", "gave up", "out of options",
            "didn't work", "wasn't working", "stopped working", "failed",
            "too expensive", "overcharged", "nickel and dime",
            "long wait", "took forever", "weeks to", "months to",
            "barely understand", "don't understand", "clueless",
            "switched from", "left my", "ditched", "dropped",
        ]
        for ev_idx, ev in enumerate(evals):
            if ev_idx < len(generated_comments):
                comment_lower = generated_comments[ev_idx].lower()
                if brand_lower in comment_lower:
                    brand_pos = comment_lower.find(brand_lower)
                    # Check ~150 chars before brand mention for negative precursors
                    pre_brand = comment_lower[max(0, brand_pos - 150):brand_pos]
                    found_negatives = [n for n in _negative_precursors if n in pre_brand]
                    if found_negatives:
                        ev["pass"] = False
                        existing_fb = ev.get("feedback", "")
                        ev["feedback"] = (
                            f"Problem-then-brand-solution arc detected: negative words "
                            f"({', '.join(found_negatives[:3])}) appear right before brand mention. "
                            "Do NOT set up a negative experience and then introduce the brand as the fix. "
                            "Place the brand in a COMPLETELY SEPARATE thought from any criticism. "
                            + existing_fb
                        ).strip()

        result["any_failed"] = any(not ev.get("pass") for ev in evals)
        return result

    def _call_claude(self, prompt, max_tokens=1024, max_retries=3, temperature=None, system_prompt=None):
        """Make API call to Claude using the official SDK with retries."""
        default_system = "You are an analytical assistant. Always respond with valid JSON only. No markdown formatting, no code blocks, no explanations — just the raw JSON object."
        system = system_prompt or default_system
        for attempt in range(max_retries):
            try:
                create_kwargs = {
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": prompt}]
                }
                if temperature is not None:
                    create_kwargs["temperature"] = temperature

                message = self.client.messages.create(**create_kwargs)

                content = message.content[0].text.strip()

                # Clean any accidental code block markers
                if content.startswith("```json"):
                    content = content[7:]
                if content.startswith("```"):
                    content = content[3:]
                if content.endswith("```"):
                    content = content[:-3]

                return json.loads(content.strip())

            except json.JSONDecodeError as e:
                print(f"    ⚠ JSON parse error (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
            except anthropic.RateLimitError:
                wait = min(2 ** attempt * 5, 60)
                print(f"    ⚠ Rate limited, waiting {wait}s...")
                time.sleep(wait)
            except anthropic.APIError as e:
                print(f"    ⚠ API error (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)

        return None

    def process_csv(
        self,
        input_csv,
        brand_name,
        brand_context,
        brand_keywords=None,
        output_csv="generated_comments.csv",
        comments_to_analyze=20,
        comments_to_generate=3,
        relevance_threshold=6,
        delay=2.0,
        skip_relevance_check=False,
        skip_validation=False
    ):
        """Process CSV and generate comments. Only writes generated results to output."""

        # Read input
        posts = []
        try:
            with open(input_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    url = row.get("url") or row.get("URL") or row.get("post_url") or row.get("link")
                    title = row.get("title") or row.get("Title") or "Unknown Title"
                    subreddit = (
                        row.get("subreddit")
                        or (self.extract_subreddit(url) if url else "unknown")
                    )

                    if url and "reddit.com" in url:
                        posts.append({"url": url, "title": title, "subreddit": subreddit})
        except Exception as e:
            print(f"❌ Error reading CSV: {e}")
            return

        if not posts:
            print("❌ No valid Reddit URLs found")
            return

        # Check for resume — skip already-processed URLs
        processed_urls = set()
        if os.path.exists(output_csv) and os.path.getsize(output_csv) > 0:
            try:
                with open(output_csv, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        processed_urls.add(row.get("post_url", ""))
                if processed_urls:
                    print(f"📋 Resuming — {len(processed_urls)} posts already processed")
            except Exception:
                pass

        # Deduplicate input URLs
        seen_urls = set()
        unique_posts = []
        for post in posts:
            if post["url"] not in seen_urls and post["url"] not in processed_urls:
                seen_urls.add(post["url"])
                unique_posts.append(post)

        skipped_dupes = len(posts) - len(unique_posts)
        if skipped_dupes > 0:
            print(f"⏭️  Skipping {skipped_dupes} duplicate/already-processed URLs")
        posts = unique_posts

        if not posts:
            print("✅ All posts already processed!")
            return

        print(f"\n{'='*70}")
        print(f"🤖 Reddit Comment Generator V3 (Improved Quality)")
        print(f"{'='*70}")
        print(f"📁 Input: {input_csv} ({len(posts)} posts to process)")
        print(f"🏷️  Brand: {brand_name}")
        print(f"📊 Relevance threshold: {relevance_threshold}/10")
        print(f"🔍 Validation: {'OFF' if skip_validation else 'ON'}")
        print(f"{'='*70}\n")

        # Set up incremental CSV writer
        fieldnames = [
            "post_url", "subreddit", "post_title", "relevance_score",
            "best_angle", "status", "tone", "strategies",
            "quality_scores"
        ]
        for j in range(1, comments_to_generate + 1):
            fieldnames.append(f"comment_{j}")
        if comments_to_generate >= 3:
            fieldnames.append("comment_3_reply_to")
            fieldnames.append("comment_3_reply_url")

        resume_mode = bool(processed_urls)
        file_mode = "a" if resume_mode else "w"
        write_header = not resume_mode

        stats = {"generated": 0, "skipped": 0, "failed": 0, "low_quality": 0, "archived": 0}
        start_time = time.time()

        with open(output_csv, file_mode, newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()

            for i, post in enumerate(posts, 1):
                elapsed = time.time() - start_time
                rate = i / elapsed if elapsed > 0 else 0
                remaining = (len(posts) - i) / rate if rate > 0 else 0
                eta = f" | ETA: {int(remaining // 60)}m{int(remaining % 60)}s" if i > 1 else ""

                print(f"\n[{i}/{len(posts)}{eta}] r/{post['subreddit']}")
                print(f"    📌 {post['title'][:60]}...")

                # Fetch comments and post body
                print(f"    📥 Fetching comments...")
                comments, post_body, is_archived = self.fetch_comments(post["url"], limit=comments_to_analyze)
                print(f"    ✓ Got {len(comments)} comments")

                # Filter archived posts
                if is_archived:
                    print(f"    ⏭️  Skipping - post is archived (can't comment)")
                    stats["archived"] += 1
                    continue

                if len(comments) < 3:
                    print(f"    ⏭️  Skipping - not enough comments")
                    stats["skipped"] += 1
                    continue

                # Compute comment statistics
                comment_stats = self._compute_comment_stats(comments)

                # Relevance check
                relevance = {"score": "N/A", "best_angle": "", "disqualified": False}
                if not skip_relevance_check:
                    print(f"    🎯 Checking relevance...")
                    relevance = self.check_relevance(
                        post["title"], post_body, post["subreddit"],
                        comments, brand_name, brand_context, brand_keywords
                    )

                    score = relevance.get("score", 0)
                    print(f"    📊 Score: {score}/10 — {relevance.get('summary', '')[:50]}")

                    if relevance.get("disqualified") or score < relevance_threshold:
                        reason = relevance.get("disqualify_reason") or relevance.get("summary", "Low relevance")
                        print(f"    ⏭️  Skipping - {'disqualified' if relevance.get('disqualified') else 'below threshold'}: {reason}")
                        stats["skipped"] += 1
                        time.sleep(delay)
                        continue

                # Analyze tone (dedicated pass)
                print(f"    🎭 Analyzing tone...")
                tone_analysis = self.analyze_tone(
                    post["title"], post_body, post["subreddit"],
                    comments, comment_stats
                )
                if tone_analysis:
                    print(f"    ✓ Tone: {tone_analysis.get('formality', '?')} | {tone_analysis.get('overall_vibe', '?')}")
                else:
                    print(f"    ⚠ Tone analysis failed, using inline fallback")

                # Select reply target for comment 3 (if generating 3+ comments)
                reply_targets = {}
                if comments_to_generate >= 3:
                    reply_target = self._select_reply_target(comments, post["title"], brand_name, relevance)
                    if reply_target:
                        reply_targets[2] = reply_target  # index 2 = comment 3
                        print(f"    💬 Reply target: u/{reply_target['author']} (score: {reply_target.get('score', '?')})")

                # Generate comments
                print(f"    ✍️  Generating comments...")
                generation = self.generate_comments(
                    post["title"], post_body, post["subreddit"],
                    comments, brand_name, brand_context,
                    num_comments=comments_to_generate,
                    tone_analysis=tone_analysis,
                    comment_stats=comment_stats,
                    relevance=relevance,
                    reply_targets=reply_targets
                )

                generated = generation.get("generated_comments", [])

                if not generated:
                    print(f"    ❌ Generation failed")
                    stats["failed"] += 1
                    time.sleep(delay)
                    continue

                # Check brand mention coverage
                with_brand = [c for c in generated if brand_name.lower() in c.lower()]
                print(f"    ✓ Brand mentioned in {len(with_brand)}/{len(generated)} comments")

                # Guarantee: if ANY comment is missing the brand, retry with specific guidance
                if len(with_brand) < len(generated):
                    missing_indices = [i+1 for i, c in enumerate(generated) if brand_name.lower() not in c.lower()]
                    print(f"    🔄 Comment(s) {missing_indices} missing brand — retrying...")
                    brand_story_feedback = (
                        f"Comment(s) {', '.join(str(i) for i in missing_indices)} did not mention {brand_name}. "
                        f"EVERY comment must include {brand_name} by name exactly once, but only as a tiny aside — "
                        f"one clause or parenthetical. Do not describe what {brand_name} does or evaluate it. "
                        f"Just name it as part of your experience, e.g. 'my doc at {brand_name}' or 'I use {brand_name} fwiw'."
                    )
                    retry_gen = self.generate_comments(
                        post["title"], post_body, post["subreddit"],
                        comments, brand_name, brand_context,
                        num_comments=comments_to_generate,
                        tone_analysis=tone_analysis,
                        comment_stats=comment_stats,
                        retry_feedback=brand_story_feedback,
                        relevance=relevance,
                        reply_targets=reply_targets
                    )
                    retry_comments = retry_gen.get("generated_comments", [])
                    if retry_comments:
                        generated = retry_comments
                        generation = retry_gen
                        with_brand_retry = [c for c in generated if brand_name.lower() in c.lower()]
                        if len(with_brand_retry) == len(generated):
                            print(f"    ✓ Brand now mentioned in all {len(generated)} comments after retry")
                        else:
                            still_missing = [i+1 for i, c in enumerate(generated) if brand_name.lower() not in c.lower()]
                            print(f"    ⚠ Comment(s) {still_missing} still missing brand — proceeding anyway")

                            # Targeted fix: if only comment 3 is missing brand, regenerate just that one
                            if len(generated) >= 3 and still_missing == [3]:
                                print(f"    🔄 Targeted retry for comment 3 only...")
                                c3_target = reply_targets.get(2)
                                c3_prompt = (
                                    f"Comment 3 is missing {brand_name}. Rewrite ONLY comment 3 as a reply to "
                                    f"u/{c3_target['author']}: \"{c3_target['body'][:300]}\" "
                                    f"Include {brand_name} once as a brief aside. Keep it natural."
                                ) if c3_target else (
                                    f"Comment 3 is missing {brand_name}. Rewrite ONLY comment 3. "
                                    f"Include {brand_name} once as a brief aside."
                                )
                                c3_gen = self.generate_comments(
                                    post["title"], post_body, post["subreddit"],
                                    comments, brand_name, brand_context,
                                    num_comments=1,
                                    tone_analysis=tone_analysis,
                                    comment_stats=comment_stats,
                                    retry_feedback=c3_prompt,
                                    relevance=relevance,
                                    reply_targets={0: c3_target} if c3_target else {}
                                )
                                c3_comments = c3_gen.get("generated_comments", [])
                                if c3_comments and brand_name.lower() in c3_comments[0].lower():
                                    generated[2] = c3_comments[0]
                                    print(f"    ✓ Comment 3 now has brand after targeted retry")

                # Quality gate
                quality_scores_str = ""
                status = "GENERATED"

                if not skip_validation:
                    print(f"    🔍 Validating quality...")
                    validation = self.validate_comments(
                        post["title"], post_body, post["subreddit"],
                        comments, brand_name, generated, tone_analysis
                    )

                    if validation:
                        evals = validation.get("evaluations", [])
                        scores = []
                        for ev in evals:
                            overall = ev.get("overall_score", "?")
                            scores.append(f"{overall}/10")
                        quality_scores_str = ", ".join(scores)

                        if validation.get("any_failed"):
                            failed_feedback = []
                            for ev in evals:
                                if not ev.get("pass"):
                                    idx = ev.get("comment_index", 0)
                                    fb = ev.get("feedback", "Improve authenticity")
                                    failed_feedback.append(f"Comment {idx + 1}: {fb}")

                            # Always remind about brand requirement in retry feedback
                            feedback_text = "\n".join(failed_feedback)
                            feedback_text += (
                                f"\n\nREMINDER: EVERY comment must mention {brand_name} by name exactly once "
                                f"as a brief aside. Do not forget this requirement."
                            )
                            print(f"    ⚠ Quality gate failed, regenerating with feedback...")

                            generation = self.generate_comments(
                                post["title"], post_body, post["subreddit"],
                                comments, brand_name, brand_context,
                                num_comments=comments_to_generate,
                                tone_analysis=tone_analysis,
                                comment_stats=comment_stats,
                                retry_feedback=feedback_text,
                                relevance=relevance,
                                reply_targets=reply_targets
                            )
                            generated = generation.get("generated_comments", [])

                            if generated:
                                # Re-check brand mentions after quality retry
                                post_retry_brand = [c for c in generated if brand_name.lower() in c.lower()]
                                if len(post_retry_brand) < len(generated):
                                    missing_after = [i+1 for i, c in enumerate(generated) if brand_name.lower() not in c.lower()]
                                    print(f"    🔄 Post-retry comment(s) {missing_after} missing brand — one more try...")
                                    brand_fix_fb = (
                                        f"Comment(s) {', '.join(str(i) for i in missing_after)} STILL missing {brand_name}. "
                                        f"This is required. Add '{brand_name}' once as a brief aside in each comment."
                                    )
                                    brand_fix_gen = self.generate_comments(
                                        post["title"], post_body, post["subreddit"],
                                        comments, brand_name, brand_context,
                                        num_comments=comments_to_generate,
                                        tone_analysis=tone_analysis,
                                        comment_stats=comment_stats,
                                        retry_feedback=brand_fix_fb,
                                        relevance=relevance,
                                        reply_targets=reply_targets
                                    )
                                    brand_fix_comments = brand_fix_gen.get("generated_comments", [])
                                    if brand_fix_comments:
                                        generated = brand_fix_comments
                                        generation = brand_fix_gen

                                print(f"    🔍 Re-validating...")
                                validation2 = self.validate_comments(
                                    post["title"], post_body, post["subreddit"],
                                    comments, brand_name, generated, tone_analysis
                                )
                                if validation2:
                                    evals2 = validation2.get("evaluations", [])
                                    scores2 = [f"{ev.get('overall_score', '?')}/10" for ev in evals2]
                                    quality_scores_str = ", ".join(scores2)

                                    if validation2.get("any_failed"):
                                        status = "GENERATED_LOW_QUALITY"
                                        stats["low_quality"] += 1
                                        print(f"    ⚠ Still below threshold — marking as low quality")
                                    else:
                                        print(f"    ✅ Retry passed quality gate!")
                                else:
                                    quality_scores_str = "validation_error"
                            else:
                                print(f"    ❌ Retry generation failed")
                                stats["failed"] += 1
                                time.sleep(delay)
                                continue
                        else:
                            print(f"    ✅ Quality gate passed ({quality_scores_str})")

                if generated:
                    if status == "GENERATED":
                        print(f"    ✅ Generated {len(generated)} comments")
                    stats["generated"] += 1

                    tone_str = ""
                    if tone_analysis and isinstance(tone_analysis, dict):
                        tone_str = f"{tone_analysis.get('formality', '?')} | {tone_analysis.get('overall_vibe', '?')}"

                    row = {
                        "post_url": post["url"],
                        "subreddit": post["subreddit"],
                        "post_title": post["title"],
                        "relevance_score": relevance.get("score", "N/A"),
                        "best_angle": relevance.get("best_angle", ""),
                        "status": status,
                        "tone": tone_str,
                        "strategies": " | ".join(generation.get("strategies_used", [])),
                        "quality_scores": quality_scores_str
                    }

                    for j, comment in enumerate(generated, 1):
                        row[f"comment_{j}"] = comment
                    for j in range(len(generated) + 1, comments_to_generate + 1):
                        row[f"comment_{j}"] = ""

                    # Add reply target info for comment 3
                    if comments_to_generate >= 3 and reply_targets.get(2):
                        target = reply_targets[2]
                        row["comment_3_reply_to"] = f"u/{target['author']}"
                        permalink = target.get("permalink", "")
                        row["comment_3_reply_url"] = f"https://reddit.com{permalink}" if permalink else ""
                    elif comments_to_generate >= 3:
                        row["comment_3_reply_to"] = ""
                        row["comment_3_reply_url"] = ""

                    writer.writerow(row)
                    f.flush()

                    # Anti-detection: record pattern fingerprints for cross-post avoidance
                    personas_used = generation.get("_personas", [])
                    structures_used = generation.get("_structures", [])
                    for cidx, comment in enumerate(generated):
                        persona_id = personas_used[cidx] if cidx < len(personas_used) else "unknown"
                        structure_id = structures_used[cidx] if cidx < len(structures_used) else "unknown"
                        fp = self._extract_pattern_fingerprint(comment, brand_name, persona_id, structure_id)
                        self._pattern_history.append(fp)

                time.sleep(delay)

        total_time = time.time() - start_time
        print(f"\n{'='*70}")
        print(f"✅ COMPLETE! ({int(total_time // 60)}m {int(total_time % 60)}s)")
        print(f"{'='*70}")
        print(f"📊 Generated: {stats['generated']} posts")
        if stats["low_quality"] > 0:
            print(f"⚠️  Low quality: {stats['low_quality']} posts (review recommended)")
        print(f"⏭️  Skipped: {stats['skipped']} posts")
        if stats["archived"] > 0:
            print(f"📦 Archived: {stats['archived']} posts")
        print(f"❌ Failed: {stats['failed']} posts")
        print(f"💾 Output: {output_csv}")
        print(f"{'='*70}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate high-quality, contextual Reddit comments",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python comment_generator.py posts.csv --domain petermd.com
  python comment_generator.py posts.csv --domain petermd.com --brand "PeterMD" (override brand name)
  python comment_generator.py posts.csv --brand "PeterMD" --context "Men's telehealth for TRT and weight loss"
        """
    )

    parser.add_argument("input_csv", help="CSV with Reddit post URLs")
    parser.add_argument("--domain", "-d", default=None, help="Brand domain to auto-extract info (e.g. petermd.com)")
    parser.add_argument("--brand", "-b", default=None, help="Brand name (required if --domain not provided)")
    parser.add_argument("--context", "-c", default=None, help="What brand does (required if --domain not provided)")
    parser.add_argument("--api-key", "-k", default=None, help="Anthropic API key")
    parser.add_argument("--keywords", "-kw", default=None, help="Comma-separated brand keywords")
    parser.add_argument("--threshold", "-t", type=int, default=6, help="Relevance threshold (default: 6)")
    parser.add_argument("--skip-relevance", action="store_true", help="Skip relevance check")
    parser.add_argument("--skip-validation", action="store_true", help="Skip quality validation (faster, cheaper)")
    parser.add_argument("--analyze", "-a", type=int, default=20, help="Comments to analyze (default: 20)")
    parser.add_argument("--generate", "-g", type=int, default=3, help="Comments to generate (default: 3, comment 3 is a reply)")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between posts (default: 2)")
    parser.add_argument("--output", "-o", default="generated_comments.csv", help="Output file")

    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌ API key required (--api-key or ANTHROPIC_API_KEY env var)")
        return

    if not os.path.exists(args.input_csv):
        print(f"❌ File not found: {args.input_csv}")
        return

    bot = CommentGeneratorBot(api_key)

    brand_keywords = [k.strip() for k in args.keywords.split(",")] if args.keywords else None

    if args.domain:
        print(f"🌐 Extracting brand info from {args.domain}...")
        brand_info = bot.extract_brand_info(args.domain)
        if not brand_info:
            print("⚠ Could not extract brand info automatically.")
            brand_name = args.brand or input("    Enter brand name: ").strip()
            brand_context = args.context or input("    Enter what brand does: ").strip()
            if not brand_name or not brand_context:
                print("❌ Brand name and context are required.")
                return
        else:
            brand_name = args.brand or brand_info["brand_name"]
            brand_context = args.context or brand_info["brand_context"]
            if not brand_keywords:
                brand_keywords = brand_info.get("brand_keywords")
        print(f"    Brand: {brand_name}")
        print(f"    Context: {brand_context}")
        if brand_keywords:
            print(f"    Keywords: {', '.join(brand_keywords)}")
    elif args.brand and args.context:
        brand_name = args.brand
        brand_context = args.context
    else:
        print("❌ Either --domain or both --brand and --context are required")
        return

    bot.process_csv(
        input_csv=args.input_csv,
        brand_name=brand_name,
        brand_context=brand_context,
        brand_keywords=brand_keywords,
        output_csv=args.output,
        comments_to_analyze=args.analyze,
        comments_to_generate=args.generate,
        relevance_threshold=args.threshold,
        delay=args.delay,
        skip_relevance_check=args.skip_relevance,
        skip_validation=args.skip_validation
    )


if __name__ == "__main__":
    main()
