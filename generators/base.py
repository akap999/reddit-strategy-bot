"""
Shared constants and Claude API client for Reddit Strategy Bot.
Extracted from comment_generator.py + 5 new personas.
"""

import anthropic
import json
import re
import time
import random
import threading
import requests

from config import DEFAULT_MODEL
from generators.pdf_text import is_base64_pdf, pdf_text_from_base64


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


def _forgiving_json_loads(s):
    """Best-effort local salvage of an ALMOST-valid JSON object string — no API call.
    Handles the cheap failure modes: stray prose/fences around the object, and a
    trailing comma before a } or ]. Does NOT try to fix unescaped inner quotes /
    control chars inside a big string value (that needs the model — see _repair_json).
    Returns the parsed object or None."""
    if not s:
        return None
    s = s.strip()
    # strip accidental code fences
    if s.startswith("```json"):
        s = s[7:]
    elif s.startswith("```"):
        s = s[3:]
    if s.endswith("```"):
        s = s[:-3]
    s = s.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # slice to the outermost {...} (drops any leading/trailing prose)
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        sub = s[i:j + 1]
        try:
            return json.loads(sub)
        except Exception:
            # remove trailing commas: ",  }" / ", ]"
            sub2 = re.sub(r",(\s*[}\]])", r"\1", sub)
            try:
                return json.loads(sub2)
            except Exception:
                pass
    return None



def _extract_json_object(text, key):
    """FU211: pull the JSON object that carries `key` out of a web_search answer.

    With the web_search tool the model routinely wraps its JSON in prose ("I'll search for…",
    "Based on my research…"), splits it across citation text blocks, or writes it twice — a bare
    json.loads then fails ("Expecting value … char 0" / "Extra data") and the whole search was
    thrown away. Tries, in order: a clean parse, the forgiving outermost-{…} salvage, then a
    raw_decode scan from every "{" (finds the object inside prose or next to a second object).
    Returns (dict, how) with how in {"clean", "salvaged"}, or (None, "") when nothing parses."""
    t = (text or "").strip()
    for fence in ("```json", "```"):
        if t.startswith(fence):
            t = t[len(fence):]
    if t.endswith("```"):
        t = t[:-3]
    t = t.strip()
    if not t:
        return None, ""
    try:
        data = json.loads(t)
        if isinstance(data, dict) and key in data:
            return data, "clean"
    except Exception:
        pass
    data = _forgiving_json_loads(t)
    if isinstance(data, dict) and key in data:
        return data, "salvaged"
    dec = json.JSONDecoder()
    starts = [m.start() for m in re.finditer(r"\{", t)][:400]
    for i in starts:
        try:
            obj, _end = dec.raw_decode(t, i)
        except Exception:
            continue
        if isinstance(obj, dict) and key in obj:
            return obj, "salvaged"
    return None, ""


def _web_citations(message):
    """FU211: the pages the model actually CITED in its answer (text-block citations of type
    web_search_result_location) as [{title, url, fact}] — fact = the cited passage. Deduped by
    url. Only citations, never uncited search hits: a result the model merely saw is not a source."""
    out, seen = [], set()
    try:
        blocks = list(getattr(message, "content", None) or [])
    except Exception:
        return out
    for block in blocks:
        btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if btype != "text":
            continue
        cites = block.get("citations") if isinstance(block, dict) else getattr(block, "citations", None)
        for c in (cites or []):
            get = (c.get if isinstance(c, dict) else (lambda k, _c=c: getattr(_c, k, None)))
            url = str(get("url") or "").strip()
            if not url or url.lower() in seen:
                continue
            seen.add(url.lower())
            out.append({"title": str(get("title") or "").strip(), "url": url,
                        "fact": re.sub(r"\s+", " ", str(get("cited_text") or "")).strip()[:500]})
    return out


class WriterClient:
    """FU153: a lightweight OpenAI-compatible caller for a SELF-HOSTED open-weight model
    (e.g. Qwen3-14B on Modal/vLLM), used ONLY for the final blog content-writing pass that
    strips a Claude SynthID watermark by making the open model the last author of the prose.

    Deliberately separate from ClaudeClient: its own model id + endpoint + key, plain HTTP via
    `requests` (mirrors PostGenerator._embed_texts — no new dependency, no anthropic SDK). Cost
    is billed by the host (Modal, by GPU-time) so there is NO usage/cost accumulator here — the
    blog's `gen_cost` stays Claude-only.

    `endpoint_url` is the OpenAI-compatible base up to and including `/v1`
    (e.g. `https://<app>.modal.run/v1`); we POST to `<endpoint_url>/chat/completions`.
    `call_text` returns the assistant message string, or None on any failure (caller falls back
    to the Claude output)."""

    def __init__(self, endpoint_url, api_key, model):
        self.endpoint_url = (endpoint_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""

    def call_text(self, prompt, system_prompt=None, max_tokens=6000, temperature=0.7, timeout=300):
        """POST one chat completion to the self-hosted endpoint. Returns the message content
        string, or None on missing config / non-200 / bad shape / exception (never raises)."""
        if not self.endpoint_url or not self.model:
            return None
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        try:
            resp = requests.post(
                f"{self.endpoint_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={"model": self.model, "messages": messages,
                      "max_tokens": max_tokens, "temperature": temperature},
                timeout=timeout,
            )
            if resp.status_code != 200:
                print(f"[writer] endpoint {resp.status_code}: {resp.text[:200]}", flush=True)
                return None
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                return None
            content = (choices[0].get("message") or {}).get("content")
            return content.strip() if isinstance(content, str) and content.strip() else None
        except Exception as e:
            print(f"[writer] endpoint error: {e}", flush=True)
            return None

    def probe(self, timeout=30):
        """FU154: quick health probe for the Settings 'Test connection' button. Returns
        {state, detail, sample?}: 'ok' (200 + a reply → endpoint AND generation work), 'warming'
        (a cold-start 3xx redirect OR a timeout → configured fine, the model is just waking),
        'auth' (401/403 → key mismatch), 'unreachable' (connection/DNS error → bad URL), or 'error'
        (other / missing config). Never raises. Distinguishes 'asleep' from 'misconfigured' and
        returns fast (a short timeout) instead of blocking on a multi-minute cold start."""
        if not self.endpoint_url or not self.model:
            return {"state": "error", "detail": "endpoint URL or model not set"}
        try:
            resp = requests.post(
                f"{self.endpoint_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": self.model, "messages": [{"role": "user", "content": "Reply with: OK"}],
                      "max_tokens": 5},
                timeout=timeout, allow_redirects=False,
            )
            sc = resp.status_code
            if sc == 200:
                try:
                    sample = ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                except Exception:
                    sample = ""
                return {"state": "ok", "detail": "endpoint answered", "sample": (sample or "").strip()[:80]}
            if sc in (301, 302, 303, 307, 308):
                return {"state": "warming",
                        "detail": "reachable, but the model is cold-starting (Modal) — retry in a few minutes; it also loads on the first blog"}
            if sc in (401, 403):
                return {"state": "auth", "detail": f"auth rejected ({sc}) — the API key doesn't match the endpoint"}
            return {"state": "error", "detail": f"HTTP {sc}: {(resp.text or '')[:160]}"}
        except requests.exceptions.Timeout:
            return {"state": "warming",
                    "detail": f"no response within {timeout}s — the model is likely cold-starting; retry shortly"}
        except Exception as e:
            return {"state": "unreachable", "detail": f"cannot reach the endpoint: {e}"}

    def warm(self, timeout=20):
        """FU164: keep-alive ping to SPIN UP / HOLD the self-hosted container warm — Modal boots the
        GPU on an incoming request and resets its idle timer, so pinging during the (long) Claude
        stages means the writer pass finds the model ALREADY LOADED (no cold start on the critical
        path). Sends a 1-token completion; a warm container answers in ~1s, a cold one starts booting
        (the client may time out but Modal keeps loading). Returns 'ok' | 'warming' | 'error'. Never raises."""
        if not self.endpoint_url or not self.model:
            return "error"
        try:
            resp = requests.post(
                f"{self.endpoint_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": self.model, "messages": [{"role": "user", "content": "ok"}],
                      "max_tokens": 1},
                timeout=timeout,
            )
            return "ok" if resp.status_code == 200 else "warming"
        except Exception:
            return "warming"   # a timeout / cold-start still triggered the boot — treat as warming, not error


class ClaudeClient:
    """Shared Claude API caller extracted from CommentGeneratorBot._call_claude."""

    # Per-1M-token (input, output) list prices, for turning accumulated usage into a
    # dollar cost (FU54). Fallback to Sonnet rates for an unknown model id.
    _MODEL_RATES = {
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-opus-4-8": (5.0, 25.0),
        "claude-opus-4-7": (5.0, 25.0),
        "claude-opus-4-6": (5.0, 25.0),
        "claude-haiku-4-5": (1.0, 5.0),
        "claude-fable-5": (10.0, 50.0),
    }
    _WEB_SEARCH_COST = 0.01           # $ per web_search server-tool request

    def __init__(self, api_key):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = DEFAULT_MODEL
        # FU54: passive per-instance usage accumulator. Every API call feeds `_track`;
        # `usage_cost()` turns it into dollars. A fresh client per blog task scopes it to
        # one generation. Callers that don't read it are unaffected.
        self._usage = {"input_tokens": 0, "output_tokens": 0, "web_search_requests": 0}
        # FU151 (B): the usage accumulator is read/written from parallel worker threads (blog_gen
        # parallelizes evidence fetches + Pass-A competitor sourcing), so guard it with a lock —
        # otherwise `+=` races drop counts and the cost ceiling reads a stale total.
        self._usage_lock = threading.Lock()
        # FU55: optional live COST CEILING ($). Once accumulated cost reaches it, further
        # web_search-backed calls (search_sources / fetch_site_facts / find_official_domain) are
        # SKIPPED, so a generation can't blow past a dollar budget. None = no cap (default).
        self._cost_ceiling = None
        # FU205 (R6) — how many web-search calls the ceiling SKIPPED. Starvation used to be
        # completely invisible: past the ceiling these three methods return []/"" and the caller
        # cannot tell "nothing was found" from "we stopped looking". The downstream symptoms — thin
        # sources, unsourced competitors, blank cells, punts, the mass-pause — are exactly the ones
        # that have been debugged as logic bugs for ~15 rounds. FU150's own context says as much:
        # competitors "used to burn the whole shared web-search budget… leaving later competitors
        # with `_over_budget()`=True on every call". The ordering was fixed; the signal never was.
        self._skipped_searches = 0

    def reset_usage(self):
        """Zero the usage accumulator (call at the start of a generation to cost it)."""
        with self._usage_lock:
            self._usage = {"input_tokens": 0, "output_tokens": 0, "web_search_requests": 0}
            self._skipped_searches = 0

    def set_cost_ceiling(self, dollars):
        """Cap web-search spend for this generation: once usage_cost() >= dollars, further
        search calls are skipped (return []/""). Pass None to disable."""
        self._cost_ceiling = dollars

    def _over_budget(self):
        """True once the generation's web-search spend has hit its ceiling. COUNTS each skip, so the
        starvation can be reported instead of silently degrading the blog (FU205 R6)."""
        over = self._cost_ceiling is not None and self.usage_cost() >= self._cost_ceiling
        if over:
            with self._usage_lock:
                self._skipped_searches += 1
        return over

    def skipped_searches(self):
        """How many web-search calls the cost ceiling skipped during this generation. 0 = healthy."""
        return int(getattr(self, "_skipped_searches", 0) or 0)

    def _track(self, message):
        """Add one API response's token + web-search usage to the accumulator. Never raises
        (usage is best-effort; a missing field just doesn't count)."""
        try:
            u = getattr(message, "usage", None)
            if not u:
                return
            stu = getattr(u, "server_tool_use", None)
            with self._usage_lock:   # FU151 (B): safe under parallel workers
                self._usage["input_tokens"] += getattr(u, "input_tokens", 0) or 0
                self._usage["output_tokens"] += getattr(u, "output_tokens", 0) or 0
                if stu:
                    self._usage["web_search_requests"] += getattr(stu, "web_search_requests", 0) or 0
        except Exception:
            pass

    def usage_cost(self):
        """Dollar cost of the accumulated usage at list prices for `self.model`
        (input+output tokens + web_search requests). Approximate (cache tokens billed at
        the standard input rate)."""
        rin, rout = self._MODEL_RATES.get(self.model, (3.0, 15.0))
        with self._usage_lock:   # FU151 (B): consistent snapshot under parallel workers
            u = dict(self._usage)
        return (u["input_tokens"] / 1e6 * rin
                + u["output_tokens"] / 1e6 * rout
                + u["web_search_requests"] * self._WEB_SEARCH_COST)

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
                self._track(message)
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
                # 1) cheap local salvage — fences / surrounding prose / trailing comma.
                salvaged = _forgiving_json_loads(last_raw_content)
                if isinstance(salvaged, dict):
                    print("    ↳ recovered via local forgiving parse", flush=True)
                    return salvaged
                # 2) model self-repair — fixes the common long-body failure (an unescaped
                #    " or newline inside a big string) that a blind re-roll reproduces.
                repaired = self._repair_json(last_raw_content, str(e), max_tokens)
                if isinstance(repaired, dict):
                    print("    ↳ recovered via model JSON repair", flush=True)
                    return repaired
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

    def _repair_json(self, raw, err, orig_max_tokens):
        """One corrective API call: hand the model its own unparseable output + the
        parser error and ask for the SAME object as valid JSON. This fixes the common
        long-body failure (an unescaped " or newline inside a big `body_markdown`
        string) that a blind re-roll keeps reproducing. Returns a dict or None; never
        raises. Bounded to a single repair attempt per parse failure."""
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            # give the repaired copy room (~same size as the original) + slack, capped
            budget = max(int(orig_max_tokens or 1024), len(raw) // 3 + 1024)
            budget = min(budget, 16000)
            fix_prompt = (
                "The text below was meant to be ONE valid JSON object but failed to parse.\n"
                f"Parser error: {err}\n\n"
                "Return the SAME object as STRICTLY VALID JSON — identical field names and "
                "values, nothing added, removed, or summarized. Escape every double-quote "
                'inside a string value as \\" and every newline inside a string as \\n. '
                "Output ONLY the raw JSON object — no prose, no code fences.\n\n"
                "TEXT TO FIX:\n" + raw
            )
            msg = self.client.messages.create(
                model=self.model,
                max_tokens=budget,
                system="You repair malformed JSON. Output ONLY the corrected raw JSON object.",
                messages=[{"role": "user", "content": fix_prompt}],
            )
            self._track(msg)
            txt = msg.content[0].text.strip()
            return _forgiving_json_loads(txt)
        except Exception as e:
            print(f"    JSON repair call failed: {type(e).__name__}: {e}", flush=True)
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
            self._track(message)
            return message.content[0].text.strip()
        except Exception as e:
            print(f"    API error: {e}")
            return None

    def search_sources(self, brief, max_searches=4, allowed_domains=None,
                       blocked_domains=None, first_party=False):
        """Use Anthropic's server-side `web_search` tool to find sources for `brief`. Returns a
        list of {title, url, fact} (deduped by url), or [] on ANY error — must never raise, so
        blog generation never breaks when off/failing.

        web_search is a SERVER tool: Anthropic runs the searches inside this single request and
        returns the completed message (search results + final text), so there is no client-side
        tool loop to manage. `allowed_domains` / `blocked_domains` are mutually exclusive — pass at
        most one.

        `first_party` (FU55): when True, this is VENDOR sourcing (pin `allowed_domains=[the vendor]`
        and pull ITS OWN pages). The default guidance says "find third-party sources — NOT the
        brands' own websites", which DIRECTLY CONTRADICTS an allowed_domains pinned to the vendor's
        own site → 0 results (the v9 `t1=0`-for-all bug). `first_party=True` swaps that guidance for
        a first-party one so the pinned search actually returns the vendor's own pricing/terms pages.
        Default False keeps every third-party / corroboration caller byte-identical.
        """
        if self._over_budget():   # FU55: cost ceiling reached → stop searching
            print("    search_sources: skipped (cost ceiling reached)", flush=True)
            return []
        tool = {"type": "web_search_20250305", "name": "web_search",
                "max_uses": int(max_searches)}
        if blocked_domains:
            tool["blocked_domains"] = list(blocked_domains)
        elif allowed_domains:
            tool["allowed_domains"] = list(allowed_domains)
        if first_party:
            guidance = (
                "Use web search to pull the OWN pages of the allowed site that answer the above — "
                "the facts the brief asks for (pricing / plans, terms and conditions, key "
                "features or capabilities). Return the SPECIFIC page URLs on that site (e.g. its "
                "pricing or terms page), NOT third-party reviews. Respond with JSON ONLY (no prose, "
                'no code fences): {"sources": [{"title": "...", "url": "...", "fact": "one specific '
                'fact stated on that page"}]}. Include only pages you actually found on that site.'
            )
        else:
            guidance = (
                "Find INDEPENDENT third-party sources (review sites, news, analyst pages — NOT the "
                "brands' own websites) that support specific factual claims for the above. Use web "
                'search, then respond with JSON ONLY (no prose, no code fences): {"sources": '
                '[{"title": "...", "url": "...", "fact": "one specific, sourced fact this page '
                'supports"}]}. Include only sources you actually found; omit anything you could not '
                "verify."
            )
        prompt = f"{brief}\n\n{guidance}"
        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=3000,
                tools=[tool],
                messages=[{"role": "user", "content": prompt}],
            )
            self._track(message)
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
        out, seen = [], set()
        data, how = _extract_json_object(text, "sources")
        if data is not None:
            for s in (data.get("sources") or []):
                if not isinstance(s, dict):
                    continue
                url = str(s.get("url") or "").strip()
                if url and url.lower() not in seen:
                    seen.add(url.lower())
                    out.append({"title": str(s.get("title") or "").strip(),
                                "url": url,
                                "fact": str(s.get("fact") or "").strip()})
            if how == "salvaged" and out:
                print(f"    web_search: recovered {len(out)} source(s) from prose-wrapped JSON",
                      flush=True)
        else:
            # FU211: no parseable JSON (prose answer / truncated) — fall back to the pages the
            # model actually cited, instead of discarding a search that found real sources.
            out = _web_citations(message)
            if out:
                print(f"    web_search: recovered {len(out)} source(s) from citations "
                      f"(answer had no parseable JSON)", flush=True)
            else:
                print(f"    web_search: could not parse sources JSON (no JSON, no citations; "
                      f"{len(text)} chars of text)", flush=True)
        if not out:
            print("    web_search: returned 0 usable sources for this brief", flush=True)
        return out

    def find_regular_price(self, brand, subject, own_domain="", retail_domains=None,
                           max_searches=2, url_hint=""):
        """FU213 (Change 4a) — find the REGULAR (non-sale) list price of `brand`'s most comparable
        product for `subject`, pinned to the brand's OWN site plus the named retailers.

        Returns {"candidates": [{url, product, price, basis, was_price}], "citations": [{title, url,
        fact}]} — the citations carry `cited_text`, which is what lets the CALLER verify the figure
        actually appears on the cited page instead of trusting the model. Never raises; returns
        empty lists on any error. The model proposes; code decides (see `_accept_price_candidate`).

        `url_hint` (Change 5, step 3): an exact page the operator pointed at — the search is told to
        read THAT page, and the caller then accepts a candidate only when its url IS that page."""
        empty = {"candidates": [], "citations": []}
        if self._over_budget():
            print("    find_regular_price: skipped (cost ceiling reached)", flush=True)
            return empty
        allowed = [d for d in ([own_domain] + list(retail_domains or [])) if d]
        tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": int(max_searches)}
        if allowed:
            tool["allowed_domains"] = allowed
        prompt = (
            f"What does {brand} charge for its most comparable {subject} product?\n\n"
            + (f"Read this exact page: {url_hint}\n\n" if url_hint else "")
            + "Rules for what counts:\n"
              "- The REGULAR list price, not a sale / deal / clearance / coupon price. If a page shows "
              'a sale price next to a "was" / "list" / "regular" price, report the was/list/regular one '
              'as `price` and the sale figure as `was_price`.\n'
              "- A single unit, or the smallest pack the brand sells. State the pack/size in `basis` "
              '(e.g. "single 9 oz bottle", "3-pack, 9 oz").\n'
              f"- The page must be {brand}'s own site or one of the named retailers, and must actually "
              f"show the figure. Do not infer, average or convert a price.\n"
              "- Quote the price exactly as the page writes it (currency symbol included).\n\n"
              "Then respond with JSON ONLY (no prose, no code fences): "
              '{"candidates": [{"url": "...", "product": "...", "price": "$24.99", '
              '"basis": "single 9 oz bottle", "was_price": ""}]}. '
              "Return an empty list if no qualifying page shows a regular price."
        )
        try:
            message = self.client.messages.create(
                model=self.model, max_tokens=2000, tools=[tool],
                messages=[{"role": "user", "content": prompt}])
            self._track(message)
        except Exception as e:
            print(f"    find_regular_price error: {e}", flush=True)
            return empty
        text = ""
        try:
            for block in (message.content or []):
                if getattr(block, "type", None) == "text":
                    text += block.text
        except Exception:
            text = ""
        cands = []
        data, _how = _extract_json_object(text, "candidates")
        for c in ((data or {}).get("candidates") or []):
            if not isinstance(c, dict):
                continue
            cands.append({"url": str(c.get("url") or "").strip(),
                          "product": str(c.get("product") or "").strip(),
                          "price": str(c.get("price") or "").strip(),
                          "basis": str(c.get("basis") or "").strip(),
                          "was_price": str(c.get("was_price") or "").strip()})
        cites = _web_citations(message)
        print(f"    find_regular_price({brand}): {len(cands)} candidate(s), {len(cites)} citation(s)",
              flush=True)
        return {"candidates": cands, "citations": cites}

    def fetch_site_facts(self, domain, brand, brief, max_searches=2):
        """FIRST-PARTY fallback: pull a brand's OWN concrete facts from its OWN site via the
        server-side `web_search` tool pinned to that domain (allowed_domains=[domain]) — used
        when a direct HTTP fetch of the site is blocked on a cloud IP. Reuses the same proven
        web_search plumbing as `search_sources`. Returns a combined facts string (each line a
        sourced fact), or "" on ANY error/empty — must never raise (generation continues)."""
        if self._over_budget():   # FU55: cost ceiling reached → stop searching
            print("    fetch_site_facts: skipped (cost ceiling reached)", flush=True)
            return ""
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
            self._track(message)
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
        facts = []
        data, how = _extract_json_object(text, "facts")
        if data is not None:
            for f in (data.get("facts") or []):
                v = str(f).strip()
                if v:
                    facts.append(v)
            if how == "salvaged" and facts:
                print(f"    fetch_site_facts: recovered {len(facts)} fact(s) from prose-wrapped "
                      f"JSON ({domain})", flush=True)
        else:
            # FU211: fall back to the passages the model cited FROM THIS SITE (the search is pinned
            # to it; the domain check keeps a stray citation elsewhere out of first-party facts).
            for c in _web_citations(message):
                cd = re.sub(r"^https?://", "", c["url"].lower()).split("/")[0]
                if (cd == domain or cd.endswith("." + domain)) and c["fact"]:
                    facts.append(c["fact"])
            if facts:
                print(f"    fetch_site_facts: recovered {len(facts)} fact(s) from citations "
                      f"({domain})", flush=True)
            else:
                print(f"    fetch_site_facts: could not parse facts JSON ({domain}; no JSON, "
                      f"no citations)", flush=True)
        return "\n".join(f"- {f}" for f in facts)

    def find_official_domain(self, brand, context=""):
        """Use the web_search tool to find a brand's OFFICIAL homepage domain (bare, no
        scheme/path). Returns "" on any error/uncertainty — never raises.

        Used to resolve a niche or same-named competitor to the RIGHT peer site (e.g.
        Profound the AI-search tool -> tryprofound.com, NOT the famous same-named
        profound.com) when the model's training-knowledge guess is unreliable."""
        if self._over_budget():   # FU55: cost ceiling reached → stop searching
            return ""
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
            self._track(message)
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
        # FU211: salvage prose-wrapped JSON. No citation fallback here — a cited URL is as likely
        # a review or directory as the official site, and a wrong domain is worse than none.
        data, _how = _extract_json_object(text, "domain")
        if data is None:
            return ""
        dom = str((data or {}).get("domain") or "").strip().lower()
        dom = dom.replace("https://", "").replace("http://", "").strip("/")
        dom = dom.split("/")[0].strip()
        return dom

    # ------------------------------------------------------------------ FU221 — research layer
    # Three narrow calls that `generators/research.py` composes. The split is deliberate (tested on
    # Railway, 19 Sep): ONE call doing search + page reading + answering left half its quotes
    # unverifiable (the model quoted pages it only saw as search results, whose text never reaches us)
    # and cost $0.75 for one brand. Search only FINDS pages; our code READS them; a tool-free call
    # EXTRACTS from text we hold — so every quote can be checked word for word, at about a third of the
    # cost. All three use the app's model, the tool versions the app already runs (no SDK upgrade), and
    # the shared usage/ceiling accounting. Never raise.

    _WEB_FETCH_TOOL = "web_fetch_20250910"
    _WEB_FETCH_BETA = "web-fetch-2025-09-10"

    def find_pages(self, brand, domains, needs, context="", max_searches=3, max_pages=4,
                   queries=None):
        """Step A: which pages on `domains` (the brand's own site, pinned) state each need. Returns a
        list of URLs (most useful first, ≤ max_pages) — never page text, never a model summary.

        FU222 `queries`: the literal searches to run, in a buyer's words ("<brand> <product> price").
        A need is a comparison COLUMN HEADING, and a heading does not match the page it is asking
        about — a page titled "Pricing" answers "<brand> pricing". The domain pin is unchanged, so
        this changes WHICH of the brand's own pages are found, never whose pages they are."""
        doms = [d for d in (domains or []) if d]
        needs = [n for n in (needs or []) if str(n).strip()]
        if not doms or not needs or self._over_budget():
            return []
        _qs = [str(q).strip() for q in (queries or []) if str(q).strip()]
        tool = {"type": "web_search_20250305", "name": "web_search",
                "max_uses": int(max_searches), "allowed_domains": doms}
        prompt = (f"Find the pages on {', '.join(doms)} ({brand}'s own site) that state each of these "
                  f"about {brand}" + (f" ({context})" if context else "") + ":\n"
                  + "\n".join(f"{i + 1}. {n}" for i, n in enumerate(needs))
                  + ("\n\nSearch for these, in these words — they are what a buyer would type:\n"
                     + "\n".join(f"- {q}" for q in _qs) if _qs else "")
                  + f"\n\nRespond with JSON only (no prose, no code fences): "
                    f'{{"pages": ["https://...", "..."]}} — at most {int(max_pages)} distinct page URLs '
                    "on that site, the most useful first. Product, pricing, plans, features, about and "
                    "policy pages beat blog posts and news. NEVER return an affiliate, partner-signup, "
                    "press, newsroom, investor or careers page — those are written for recruiters and "
                    "reporters, and the fact a reader needs is on the product or policy page. Return "
                    "an empty list if none exist.")
        try:
            message = self.client.messages.create(
                model=self.model, max_tokens=800, tools=[tool],
                messages=[{"role": "user", "content": prompt}])
            self._track(message)
        except Exception as e:
            print(f"    find_pages error ({brand}): {e}", flush=True)
            return []
        text = "".join(getattr(b, "text", "") for b in (message.content or [])
                       if getattr(b, "type", None) == "text")
        data, _how = _extract_json_object(text, "pages")
        urls, seen = [], set()
        for u in ((data or {}).get("pages") or []):
            u = str(u or "").strip()
            key = u.lower().split("#")[0].rstrip("/")
            if u.startswith(("http://", "https://")) and key not in seen:
                seen.add(key)
                urls.append(u)
        if not urls:   # fall back to the pages the search itself surfaced
            for c in _web_citations(message):
                cu = c.get("url") or ""
                key = cu.lower().split("#")[0].rstrip("/")
                if cu and key not in seen:
                    seen.add(key)
                    urls.append(cu)
        return urls[:int(max_pages)]

    def web_fetch_text(self, url, max_content_tokens=8000):
        """Read ONE page through Anthropic's web fetch tool (it runs on Anthropic's servers, so a site
        that walls our IP often still serves it — tested: Capterra, jollysearch.com, osborneslaw.com,
        lowes.com, TrustRadius). Returns (text, "ok") or ("", <error code>). The URL is in the prompt,
        which satisfies the tool's "URL must already be in the conversation" rule. `max_tokens` must
        leave room for the tool call itself (a tiny value cuts it off before it names the URL)."""
        if not url:
            return "", "no-url"
        if self._over_budget():
            return "", "budget"
        tool = {"type": self._WEB_FETCH_TOOL, "name": "web_fetch", "max_uses": 1,
                "max_content_tokens": int(max_content_tokens)}
        try:
            message = self.client.messages.create(
                model=self.model, max_tokens=400, tools=[tool],
                messages=[{"role": "user", "content": f"Fetch {url} and reply only OK."}],
                extra_headers={"anthropic-beta": self._WEB_FETCH_BETA})
            self._track(message)
        except Exception as e:
            print(f"    web_fetch error ({url}): {e}", flush=True)
            return "", "error"
        try:
            blocks = message.model_dump(warnings=False).get("content") or []
        except Exception:
            blocks = []
        for b in blocks:
            if (b or {}).get("type") != "web_fetch_tool_result":
                continue
            c = b.get("content") or {}
            if c.get("type") == "web_fetch_result":
                data = (((c.get("content") or {}).get("source") or {}).get("data")) or ""
                if data.strip():
                    # FU227: web fetch does NOT read a PDF — it hands the file back base64-encoded.
                    # Left alone, megabytes of base64 entered the evidence as though they were the
                    # page's text. Decode and read it, or say plainly that it could not be read.
                    if is_base64_pdf(data):
                        _t = pdf_text_from_base64(data)
                        return (_t, "ok") if _t else ("", "pdf-unreadable")
                    return data, "ok"
                return "", "empty"
            return "", str(c.get("error_code") or "error")
        return "", "no-fetch"

    def extract_facts(self, brand, needs, pages, context="", guidance="", page_chars=12000):
        """Step C: answer each need from ONLY the given page texts ({url: text}), with the exact quote
        and the url it came from. No tools, so the model can only use text we hold. Returns a list of
        {"need": <1-based index>, "answer", "url", "quote", "product"} ([] on any failure)."""
        needs = [n for n in (needs or []) if str(n).strip()]
        pages = {u: t for u, t in (pages or {}).items() if u and (t or "").strip()}
        if not needs or not pages:
            return []
        blob = "\n\n".join(f"=== PAGE {u}\n{t[:int(page_chars)]}" for u, t in pages.items())
        prompt = (
            f"From ONLY the page texts below, answer each item about {brand}"
            + (f" ({context})" if context else "") + ".\n"
            + "\n".join(f"{i + 1}. {n}" for i, n in enumerate(needs))
            + "\n\nRules:\n"
              "- Use only what the page text states. Never use outside knowledge, never infer.\n"
              "- `quote`: the exact sentence or line from the page that states it, copied character "
              "for character (no paraphrase, no added formatting).\n"
              "- Keep every condition the page attaches (first month, per month, billed annually, "
              "membership required, with insurance, pack size, which plan or product line).\n"
              "- `product`: the specific product, plan or line the fact belongs to, when the page "
              "names one.\n"
              "- `basis` (prices only): what the price covers and its conditions, short, using ONLY "
              "words the page itself states — e.g. \"per month, billed monthly, membership "
              "required\", \"2-pack, 5.4 oz\". Never add a unit, term or condition the page does "
              "not state; empty when it states none, and for anything that is not a price.\n"
              "- A price is the REGULAR price, never a sale / deal / discounted figure.\n"
              "- When an item names a specific product, plan or line, answer ONLY from facts about "
              "THAT one. If the page states the fact for a DIFFERENT product or plan of the same "
              "brand (a separate subscription, another tier, another size), answer \"not found\" "
              "rather than crossing them — its price, what it includes, and where it is available "
              "all belong to it, not to the item you were asked about.\n"
              "- When a price item has SEVERAL figures (an introductory price and the ongoing one, "
              "or several plans, tiers, terms or pack sizes), return a SEPARATE object for EACH "
              "figure — the same `need` number, its own `answer`, its own exact `quote`, its own "
              "`basis`. Do not merge them into one answer and do not leave any out. NEVER put two "
              "figures in one `answer` (not even in a parenthesis): if you would write \"$39/mo "
              "(billed annually: $32.49/mo)\", return $39 with the basis that belongs to IT and "
              "$32.49 as its own object with ITS basis.\n"
            + (f"- {guidance}\n" if guidance else "")
            + "- If the pages do not state an item, answer \"not found\" with an empty quote.\n\n"
              'Respond with JSON only: {"facts": [{"need": 1, "answer": "...", "url": "...", '
              '"quote": "...", "product": "", "basis": ""}]}\n\n' + blob)
        data = self.call(prompt, max_tokens=2000, temperature=0)
        out = []
        for f in ((data or {}).get("facts") or []) if isinstance(data, dict) else []:
            if not isinstance(f, dict):
                continue
            try:
                idx = int(f.get("need") or f.get("item") or 0)
            except (TypeError, ValueError):
                idx = 0
            out.append({"need": idx, "answer": str(f.get("answer") or "").strip(),
                        "url": str(f.get("url") or "").strip(),
                        "quote": str(f.get("quote") or "").strip(),
                        "product": str(f.get("product") or "").strip(),
                        "basis": str(f.get("basis") or "").strip()})
        return out
