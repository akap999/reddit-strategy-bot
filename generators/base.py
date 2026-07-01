"""
Shared constants and Claude API client for Reddit Strategy Bot.
Extracted from comment_generator.py + 5 new personas.
"""

import anthropic
import json
import time
import random

from config import DEFAULT_MODEL


# --- Anti-detection: Persona pool (20 total) ---
PERSONAS = [
    {
        "id": "skeptic",
        "voice": "Independent thinker. Pushes back on the consensus advice with their own counter-take. States the disagreement directly and the reason behind it. NEVER opens with disclaimers — no 'take this with a grain of salt', no 'idk if this works', no 'not saying it's the answer but'. Just states what they think and why.",
        "length": "medium",
    },
    {
        "id": "newbie",
        "voice": "Recently got into this. Asks one specific concrete follow-up question or shares a fresh-eyes observation about something in the post. NEVER opens with 'still learning' / 'not sure if this is normal' / 'anyone else'. Just asks the question or makes the observation directly.",
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
        "voice": "Brief, almost first-post energy — direct but a bit awkward in phrasing (occasional fragment, no smooth transitions). NEVER opens with 'usually just lurk', 'made an account to say', 'don't normally post', or any disclaimer. Just gets to the point a touch ungracefully.",
        "length": "medium",
    },
    {
        "id": "frustrated",
        "voice": "Tired/resigned tone about the SITUATION (the post's problem), not about specific brands or tools. States the frustration concretely with what specifically isn't working. NEVER uses 'still figuring it out', 'nothing works the way you expect', 'this whole thing is exhausting' — those are AI tells. Just describes the actual frustration in concrete terms.",
        "length": "medium-long",
    },
    {
        "id": "helper",
        "voice": "You're primarily here to help the OP. Your entire focus is giving useful advice. Any personal details are secondary. Direct, supportive tone.",
        "length": "medium",
    },
    {
        "id": "comparer",
        "voice": "You've tried 2-3 different options and give honest pros/cons of each. No winner, no favorite — each has trade-offs. Use phrases like 'tried X first', 'then Y', 'currently on Z'. Neutral, slightly resigned. IMPORTANT: if any of the options you compare is the brand the comment is meant to mention, do NOT criticize it — describe it neutrally, save any complaints for the OTHER options or skip them entirely.",
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
        "voice": "Switched from one option to another recently. Gives a concrete before/after: what the old thing did, what the new thing does, the specific functional difference. Even-handed about both, neither rosy nor bitter. NEVER hedges with 'honestly not sure it was worth', 'might switch back honestly', or 'idk'. Just describes the switch with specifics.",
        "length": "medium-long",
    },
    {
        "id": "long_timer",
        "voice": "You've been doing this for over a year. Very casual about everything — it's just part of your routine now, not exciting or new. Phrases like 'been on this for like a year', 'at this point it's just', 'nothing dramatic to report'. Understated, low-energy.",
        "length": "short-medium",
    },
    # --- 5 New Personas ---
    {
        "id": "professional",
        "voice": "You work in or adjacent to the industry. You speak from professional knowledge but casually. Phrases like 'from a clinical standpoint', 'in my experience working with', 'professionally speaking'. You don't flaunt credentials but your expertise shows.",
        "length": "medium-long",
    },
    {
        "id": "parent",
        "voice": "You frame everything through how it affects your family or kids. Phrases like 'as a dad of two', 'my wife noticed', 'hard to find time with kids'. Practical, family-first perspective. Relatable and grounded.",
        "length": "medium",
    },
    {
        "id": "researcher",
        "voice": "You read studies and papers before trying things. Ask for sources. Phrases like 'the data suggests', 'I read a study that', 'do you have a source for that?'. Methodical but not condescending. You want evidence.",
        "length": "medium-long",
    },
    {
        "id": "impatient",
        "voice": "You want quick answers and don't have time for long explanations. Phrases like 'tldr?', 'just tell me what works', 'skip the backstory'. Skims threads, gives brief takes. Slightly blunt but not rude.",
        "length": "short",
    },
    {
        "id": "grateful",
        "voice": "Appreciative tone, follows up with thanks. Phrases like 'this is really helpful', 'appreciate the detailed response', 'exactly what I needed to hear'. You're genuinely thankful for community help. Warm but not over the top.",
        "length": "short-medium",
    },
]

# --- Anti-detection: Comment structure templates ---
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
        "instruction": "ONLY use this framing if the EXISTING COMMENTS above contain clear advice or suggestions you could plausibly follow up on. When they do, frame your comment as an update ('tried what X suggested', 'update on this since I was in the same boat') and reference a specific piece of advice actually present in the comments. If the existing comments don't contain suggestions to follow up on — or there are no existing comments at all — DO NOT use this framing. Pick a different angle grounded in the actual post content.",
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


# --- Anti-detection: Banned phrases (marketing fingerprints + AI tells) ---
BANNED_PHRASES = [
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
    "makes a good point", "makes a solid point",
    "is spot on", "is absolutely right", "is right about",
    "honestly",
    "actually",
    " — ", " -- ", " - ",
    "don't post much", "don't usually post", "don't normally post",
    "hit close to home", "hits close to home",
    # --- Hedging / disclaimer openers (the "take this with a grain of
    # salt" family — explicit AI-shilly tells the user has flagged) ---
    "take this with a grain of salt", "grain of salt",
    "your mileage may vary", "ymmv",
    "for what it's worth", "fwiw",
    "just my two cents", "my two cents",
    "not sure if this helps", "not sure if this helps but",
    "could be wrong but", "i could be wrong",
    "feel free to ignore",
    "take what i say with",
    "still figuring it out", "still figuring out",
    "still learning",
    "not saying it's the answer", "not saying its the answer",
    "idk if this works for everyone", "not sure if this is normal",
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
FEW_SHOT_POOL = [
    {
        "post": "Anyone else dealing with brain fog lately?",
        "bad": "I've been using BrandX and it really helped with my brain fog. Highly recommend checking them out.",
        "why_bad": "Entire comment is about the brand. No real advice. Reads like an ad.",
    },
    {
        "post": "What bloodwork should I ask for?",
        "bad": "BrandX does comprehensive bloodwork and their doctors are really knowledgeable about what to test for!",
        "why_bad": "Doesn't answer the question at all. Just redirects to brand. Zero useful info.",
    },
    {
        "post": "Best online options? Overwhelmed by choices",
        "bad": "BrandX is the best option out there. Their prices are competitive and the service is amazing.",
        "why_bad": "Every sentence is brand praise. 'Best', 'competitive', 'amazing' are shill words. No comparison, no nuance.",
    },
    {
        "post": "Is this normal or should I be worried?",
        "bad": "Don't worry, BrandX helped me with the same thing and their doctors are super responsive!",
        "why_bad": "Dismisses concern with brand plug. 'Super responsive' is a marketing phrase. No empathy or real reassurance.",
    },
    {
        "post": "My doctor keeps dismissing my concerns",
        "bad": "Switch to BrandX! They actually listen to their patients and the process was super easy.",
        "why_bad": "Exclamation mark enthusiasm. 'Actually listen' and 'super easy' are promotional. Sounds like a sales pitch not a frustrated patient.",
    },
    {
        "post": "How long before you noticed changes?",
        "bad": "BrandX worked great for me, I noticed results pretty quickly!",
        "why_bad": "No specifics at all. 'Worked great' and 'pretty quickly' are meaningless. Real users give timelines and details.",
    },
    {
        "post": "Anyone else feel worse before feeling better?",
        "bad": "Yes but BrandX helped me get through it with their great medical support and monitoring!",
        "why_bad": "'Great medical support and monitoring' is straight from a brochure. Real users don't talk like this.",
    },
    {
        "post": "The cost of all this is insane",
        "bad": "BrandX is really affordable compared to other options, definitely check them out!",
        "why_bad": "'Affordable', 'definitely check them out' is an ad. Doesn't engage with the frustration about cost.",
    },
    {
        "post": "Thinking about switching providers",
        "bad": "Switch to BrandX, I made the switch and it was the best decision!",
        "why_bad": "'Best decision' is a review-site cliche. Imperative 'Switch to BrandX' is a direct sales push.",
    },
    {
        "post": "Is telehealth legit for this?",
        "bad": "Yes! BrandX is an amazing telehealth service that I highly recommend!",
        "why_bad": "'Amazing', 'highly recommend', double exclamation. Every bot flag in one sentence.",
    },
    {
        "post": "Local clinic vs online - what's better?",
        "bad": "I use BrandX and they have great doctors who monitor everything and adjust your protocol based on labs and they're really responsive to messages too.",
        "why_bad": "Brand name followed by multiple clauses describing what they do. 'and they... and they...' continuation is the #1 bot tell. Brand should be mentioned and immediately dropped.",
    },
    {
        "post": "Just got my first set of results back and I'm confused",
        "bad": "BrandX doctors helped me understand my results, they're really knowledgeable!",
        "why_bad": "Brand is the subject and hero of every sentence. Real users talk about their experience, not the company.",
    },
    {
        "post": "How do you explain this to your partner?",
        "bad": "BrandX makes the process really easy and discreet, my partner barely noticed!",
        "why_bad": "'Makes the process easy and discreet' is feature marketing. Comment doesn't share any real personal experience.",
    },
    {
        "post": "Is it worth the money?",
        "bad": "BrandX is really affordable and worth every penny!",
        "why_bad": "'Worth every penny' is a cliche review phrase. No actual cost numbers, no tradeoffs, no real thought.",
    },
    {
        "post": "Everyone says X is the best approach",
        "bad": "Actually BrandX offers a better approach that I'd recommend!",
        "why_bad": "'Actually BrandX offers' positions brand as the superior option. Real disagreement is about the approach, not about pushing a provider.",
    },
    {
        "post": "My doctor keeps dismissing my symptoms",
        "bad": "My old doctor was the same way, totally useless. Switched to BrandX and they actually ran the right tests. Night and day difference.",
        "why_bad": "Classic marketing testimonial structure: [negative experience with alternative] -> [brand as solution] -> [positive outcome]. The brand is positioned as the hero that solved the problem. Real users don't structure stories this way.",
    },
    {
        "post": "Should I go with a local option or online?",
        "bad": "Online is way more convenient and saves so much time. I use BrandX and never have to deal with scheduling hassles or long waits anymore.",
        "why_bad": "Frames the brand's entire category as superior, then names the brand as the specific choice. This is two layers of marketing: category positioning + brand positioning. Real users mention tradeoffs for both.",
    },
]


def select_few_shot_examples(n=3):
    """Anti-patterns only.

    A previous iteration of this code mixed in positive "good" examples,
    but concrete positive examples create template homogenization — the
    model picks up the literal opener phrasing, sentence rhythm, and
    cadence of the example and reproduces variations of it across the
    batch. Anti-patterns are different: they teach the model what to
    AVOID without prescribing a specific shape, so the model is free to
    write naturally varied comments within the constraints.

    Shape guidance (intent-driven length, sentence-count target,
    answer-vs-anecdote framing) is provided as ABSTRACT RULES inside the
    prompt's per-comment LENGTH/STRUCTURE/ANGLE sections instead.
    """
    selected = random.sample(FEW_SHOT_POOL, min(n, len(FEW_SHOT_POOL)))
    lines = ["EXAMPLES OF WHAT NOT TO DO (avoid these patterns completely):"]
    for i, ex in enumerate(selected, 1):
        lines.append(f"\n--- Anti-Pattern {i} ---")
        lines.append(f'POST: "{ex["post"]}"')
        lines.append(f'BAD COMMENT: "{ex["bad"]}"')
        lines.append(f"WHY IT'S BAD: {ex['why_bad']}")
    return "\n".join(lines)


class ClaudeClient:
    """Shared Claude API caller extracted from CommentGeneratorBot._call_claude."""

    def __init__(self, api_key):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = DEFAULT_MODEL

    def call(self, prompt, max_tokens=1024, max_retries=3, temperature=None, system_prompt=None):
        """Make API call to Claude with retries. Returns parsed JSON or None.

        On every retry failure we now stash the last exception text on
        `self.last_error` so callers can surface it (the previous
        behaviour silently returned None after retries, making
        downstream 'generation failed' errors opaque).
        """
        default_system = "You are an analytical assistant. Always respond with valid JSON only. No markdown formatting, no code blocks, no explanations — just the raw JSON object."
        system = system_prompt or default_system
        self.last_error = None
        last_raw_content = None

        for attempt in range(max_retries):
            try:
                create_kwargs = {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": prompt}]
                }
                if temperature is not None:
                    create_kwargs["temperature"] = temperature

                message = self.client.messages.create(**create_kwargs)
                content = message.content[0].text.strip()
                last_raw_content = content

                # Clean any accidental code block markers
                if content.startswith("```json"):
                    content = content[7:]
                if content.startswith("```"):
                    content = content[3:]
                if content.endswith("```"):
                    content = content[:-3]

                return json.loads(content.strip())

            except json.JSONDecodeError as e:
                self.last_error = f"JSON parse error: {e}"
                preview = (last_raw_content or "")[:200].replace("\n", " ")
                print(f"    JSON parse error (attempt {attempt + 1}/{max_retries}): {e} | body[:200]={preview!r}", flush=True)
                if attempt < max_retries - 1:
                    time.sleep(1)
            except anthropic.RateLimitError as e:
                self.last_error = f"Rate limit: {e}"
                wait = min(2 ** attempt * 5, 60)
                print(f"    Rate limited (attempt {attempt + 1}/{max_retries}), waiting {wait}s: {e}", flush=True)
                time.sleep(wait)
            except anthropic.APIError as e:
                # Map common 4xx auth/quota errors to a clearer
                # actionable message instead of dumping the raw
                # exception. The user's most common 401/429/402
                # paths now read as 'fix this knob' rather than
                # 'go look up an error code'.
                emsg = str(e).lower()
                if "invalid x-api-key" in emsg or "authentication_error" in emsg or "401" in emsg:
                    self.last_error = (
                        "Anthropic API key is invalid or unset. "
                        "Set ANTHROPIC_API_KEY in the environment "
                        "(or .env) to a valid key from "
                        "https://console.anthropic.com/settings/keys "
                        "and restart the app."
                    )
                    # Auth errors don't recover on retry — bail.
                    print(f"    {self.last_error} (raw: {e})", flush=True)
                    return None
                elif "credit balance" in emsg or "402" in emsg or "insufficient" in emsg:
                    self.last_error = (
                        "Anthropic credit balance is too low to "
                        "complete this request. Top up the account "
                        "at https://console.anthropic.com/settings/billing."
                    )
                    print(f"    {self.last_error} (raw: {e})", flush=True)
                    return None
                else:
                    self.last_error = f"API error: {e}"
                    print(f"    API error (attempt {attempt + 1}/{max_retries}): {e}", flush=True)
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
            except Exception as e:
                # Auth errors, network, etc. — previously bubbled up
                # uncaught and the caller saw a generic
                # 'Topic generation failed' downstream.
                self.last_error = f"{type(e).__name__}: {e}"
                print(f"    unexpected error (attempt {attempt + 1}/{max_retries}): {type(e).__name__}: {e}", flush=True)
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)

        return None

    def call_text(self, prompt, max_tokens=1024, temperature=None, system_prompt=None):
        """Make API call and return raw text (not JSON). For non-JSON responses."""
        default_system = "You are a helpful assistant."
        system = system_prompt or default_system

        try:
            create_kwargs = {
                "model": self.model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": prompt}]
            }
            if temperature is not None:
                create_kwargs["temperature"] = temperature

            message = self.client.messages.create(**create_kwargs)
            return message.content[0].text.strip()
        except Exception as e:
            print(f"    API error: {e}")
            return None

    def search_sources(self, brief, max_searches=4, allowed_domains=None,
                       blocked_domains=None):
        """Use Anthropic's server-side `web_search` tool to find INDEPENDENT third-party
        sources for `brief`. Returns a list of {title, url, fact} (deduped by url), or []
        on ANY error — must never raise, so blog generation never breaks when off/failing.

        web_search is a SERVER tool: Anthropic runs the searches inside this single
        request and returns the completed message (search results + final text), so there
        is no client-side tool loop to manage. `allowed_domains` / `blocked_domains` are
        mutually exclusive — pass at most one (we use blocked_domains to exclude the brands'
        OWN sites and let the whole web answer, which surfaces real independent coverage).
        """
        tool = {"type": "web_search_20250305", "name": "web_search",
                "max_uses": int(max_searches)}
        if blocked_domains:
            tool["blocked_domains"] = list(blocked_domains)
        elif allowed_domains:
            tool["allowed_domains"] = list(allowed_domains)
        prompt = (
            f"{brief}\n\nFind INDEPENDENT third-party sources (review sites, news, analyst "
            "pages — NOT the brands' own websites) that support specific factual claims for "
            "the above. Use web search, then respond with JSON ONLY (no prose, no code "
            'fences): {"sources": [{"title": "...", "url": "...", "fact": "one specific, '
            'sourced fact this page supports"}]}. Include only sources you actually found; '
            "omit anything you could not verify."
        )
        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                tools=[tool],
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            print(f"    web_search error: {e}", flush=True)
            return []
        # Concatenate the assistant's text blocks (the final JSON answer); the search
        # result blocks are non-text and ignored here.
        text = ""
        try:
            for block in (message.content or []):
                if getattr(block, "type", None) == "text":
                    text += block.text
        except Exception:
            text = ""
        text = text.strip()
        for fence in ("```json", "```"):
            if text.startswith(fence):
                text = text[len(fence):]
        if text.endswith("```"):
            text = text[:-3]
        out, seen = [], set()
        try:
            data = json.loads(text.strip())
            for s in (data.get("sources") or []):
                if not isinstance(s, dict):
                    continue
                url = str(s.get("url") or "").strip()
                if url and url.lower() not in seen:
                    seen.add(url.lower())
                    out.append({"title": str(s.get("title") or "").strip(),
                                "url": url,
                                "fact": str(s.get("fact") or "").strip()})
        except (json.JSONDecodeError, TypeError, AttributeError) as e:
            print(f"    web_search: could not parse sources JSON: {e}", flush=True)
        if not out:
            print("    web_search: returned 0 usable sources for this brief", flush=True)
        return out

    def fetch_site_facts(self, domain, brand, brief, max_searches=2):
        """FIRST-PARTY fallback: pull a brand's OWN concrete facts from its OWN site via the
        server-side `web_search` tool pinned to that domain (allowed_domains=[domain]) — used
        when a direct HTTP fetch of the site is blocked on a cloud IP. Reuses the same proven
        web_search plumbing as `search_sources`. Returns a combined facts string (each line a
        sourced fact), or "" on ANY error/empty — must never raise (generation continues)."""
        domain = re.sub(r"^https?://", "", str(domain or "").strip().lower()).strip("/").split("/")[0]
        brand = (brand or "").strip()
        if not domain:
            return ""
        tool = {"type": "web_search_20250305", "name": "web_search",
                "max_uses": int(max_searches), "allowed_domains": [domain]}
        prompt = (
            f'From {domain} — the OFFICIAL website of "{brand or domain}" — extract its concrete, '
            f'specific facts relevant to: {brief}. Look across its pages (homepage, pricing/plans, '
            "features/product, terms/license, about) and capture real specifics: prices and plan "
            "names, what each plan includes, licensing / commercial-use / rights terms, key features "
            "and capabilities, and any other specifics that answer the brief (for a physical-goods "
            "site that also means shipping/returns/financing/locations). Use web search restricted to "
            'that site, then respond with JSON ONLY (no prose, no code fences): '
            '{"facts": ["one specific fact", "..."]}. Include only facts actually stated on the site; '
            "omit anything you cannot find there."
        )
        try:
            message = self.client.messages.create(
                model=self.model, max_tokens=1500, tools=[tool],
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            print(f"    fetch_site_facts error ({domain}): {e}", flush=True)
            return ""
        text = ""
        try:
            for block in (message.content or []):
                if getattr(block, "type", None) == "text":
                    text += block.text
        except Exception:
            text = ""
        text = text.strip()
        for fence in ("```json", "```"):
            if text.startswith(fence):
                text = text[len(fence):]
        if text.endswith("```"):
            text = text[:-3]
        facts = []
        try:
            data = json.loads(text.strip())
            for f in (data.get("facts") or []):
                v = str(f).strip()
                if v:
                    facts.append(v)
        except (json.JSONDecodeError, TypeError, AttributeError) as e:
            print(f"    fetch_site_facts: could not parse facts JSON ({domain}): {e}", flush=True)
        return "\n".join(f"- {f}" for f in facts)

    def find_official_domain(self, brand, context=""):
        """Use the web_search tool to find a brand's OFFICIAL homepage domain (bare, no
        scheme/path). Returns "" on any error/uncertainty — never raises.

        Used to resolve a niche or same-named competitor to the RIGHT peer site (e.g.
        Profound the AI-search tool -> tryprofound.com, NOT the famous same-named
        profound.com) when the model's training-knowledge guess is unreliable."""
        brand = (brand or "").strip()
        if not brand:
            return ""
        tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
        ctx = f" It operates in this space: {context.strip()}." if (context or "").strip() else ""
        prompt = (
            f'Find the OFFICIAL homepage of the product/company "{brand}".{ctx} It MUST be '
            "the brand's OWN website (its product homepage) — NOT a review site, directory, "
            "app store, news article, social profile, or a same-named company in a different "
            "industry. Use web search, then respond with JSON ONLY (no prose, no code "
            'fences): {"domain": "example.com"} — bare domain, no https://, no path. If you '
            'cannot confidently identify the official site, return {"domain": ""}.'
        )
        try:
            message = self.client.messages.create(
                model=self.model, max_tokens=600, tools=[tool],
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            print(f"    find_official_domain error: {e}", flush=True)
            return ""
        text = ""
        try:
            for block in (message.content or []):
                if getattr(block, "type", None) == "text":
                    text += block.text
        except Exception:
            text = ""
        text = text.strip()
        for fence in ("```json", "```"):
            if text.startswith(fence):
                text = text[len(fence):]
        if text.endswith("```"):
            text = text[:-3]
        try:
            data = json.loads(text.strip())
        except (json.JSONDecodeError, TypeError, AttributeError):
            return ""
        dom = str((data or {}).get("domain") or "").strip().lower()
        dom = dom.replace("https://", "").replace("http://", "").strip("/")
        dom = dom.split("/")[0].strip()
        return dom
