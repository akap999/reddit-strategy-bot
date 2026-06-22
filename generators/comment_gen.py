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
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Intent → target avg_words. Used by _classify_post_intent below to
# scale comment length to what the post actually calls for.
INTENT_AVG_WORDS = {
    "crisp": 25,    # recommendation-seeking: 12-50 words target range
    "medium": 50,   # comparison / short informational: 25-90
    "long": 90,     # experience-sharing / deep informational: 50-160
}

# Recommendation-signal lexicon. Single source of truth — generator,
# validator, length classifier, and persona-variety rules all share this.
_REC_SIGNALS = [
    "best ", "recommend", "suggest", "looking for", "any tools",
    "any apps", "what's a good", "what is the best", "what's the best",
    "anyone use", "anyone using", "anyone tried", "help me find",
    "help finding", "any tips", "any advice", "how do i", "how do you",
    "what do you use", "what would you", "which ", "alternatives to",
    "alternative for", "tool for", "app for", "platform for",
]

# Personas categorised for the anti-similarity variety pass. A batch of
# N comments shouldn't be all DIRECT or all ANECDOTE — real Reddit
# threads have a mix.
PERSONA_CATEGORIES = {
    "DIRECT": {"helper", "comparer", "professional", "data_nerd",
               "veteran_terse", "impatient", "concerned",
               "budget_conscious", "researcher"},
    "ANECDOTE": {"tangent", "parent", "long_timer", "switcher",
                 "agreeable", "lurker", "dry_humor", "grateful"},
    "SKEPTIC": {"skeptic", "frustrated", "newbie"},
}

# Curated AI-crawl + recommendation persona/structure pool — direct,
# answer-shaped voices.
AI_CRAWL_REC_PERSONAS = {"helper", "comparer", "professional",
                          "data_nerd", "veteran_terse"}
AI_CRAWL_REC_STRUCTURES = {"direct_answer", "list_format",
                            "comparison", "short_punchy"}


# ---------------------------------------------------------------------------
# HQ reply shapes — short, dynamic, conversational replies.
#
# Replies in HQ clusters were coming back as 90-word paragraphs because they
# (a) inherited the post's intent-driven length scaling, (b) drew personas
# from the full top-level pool (including data_nerd, comparer, professional —
# all long-form), and (c) got a composite "agree AND add detail AND reference
# AND don't change topic" angle prompt that nudged the model to do all of it
# at once.
#
# The fix is shape-driven: each reply slot picks ONE conversational shape
# (one-liner agree, short pushback, follow-up Q, short add, dry one-liner,
# rare medium add). The shape constrains persona pool, sentence/word target,
# and angle to a single move executed briefly.
# ---------------------------------------------------------------------------

HQ_REPLY_PERSONAS = {
    "veteran_terse", "agreeable", "dry_humor", "impatient", "grateful",
    "lurker", "long_timer", "skeptic", "newbie", "concerned",
}

HQ_REPLY_STRUCTURES = {
    "reply_to_commenter", "short_punchy",
    "question_plus_experience", "direct_answer",
}

# Each shape: tuple of (id, weight, persona-pool, structure-pool,
# sent_target, lo_words, hi_words, angle).
HQ_REPLY_SHAPES = [
    (
        "oneliner_agree", 25,
        {"agreeable", "grateful", "long_timer"},
        {"short_punchy", "reply_to_commenter"},
        "1 sentence", 8, 18,
        "ONE LINE. Cosign or agree with the TARGET COMMENT and add ONE small "
        "personal touch (a phrase, a reference, a tiny detail). No second "
        "clause, no 'but', no new argument. End at one period.",
    ),
    (
        "short_pushback", 20,
        {"skeptic", "dry_humor", "veteran_terse"},
        {"short_punchy", "reply_to_commenter", "direct_answer"},
        "1-2 sentences", 15, 30,
        "Push back briefly on ONE specific claim or phrase from the TARGET "
        "COMMENT. State what you disagree with and why, in 1-2 sentences. "
        "No throat-clearing, no 'I respectfully disagree', just the "
        "counter-take. Stay short — under 30 words.",
    ),
    (
        "followup_question", 20,
        {"newbie", "concerned", "impatient"},
        {"reply_to_commenter", "question_plus_experience", "short_punchy"},
        "1-2 sentences", 10, 25,
        "Ask ONE concrete follow-up question about something specific the "
        "TARGET COMMENT mentioned. No preamble, no 'just curious', just the "
        "question. 1-2 sentences max. End with a question mark.",
    ),
    (
        "short_add", 20,
        {"veteran_terse", "lurker", "long_timer"},
        {"reply_to_commenter", "direct_answer", "short_punchy"},
        "2-3 sentences", 25, 45,
        "Add ONE concrete detail or anecdote that builds on the TARGET "
        "COMMENT's point. 2-3 sentences. One specific number, name, or "
        "scenario — not a general principle. No 'in my experience' "
        "throat-clearing.",
    ),
    (
        "dry_oneliner", 10,
        {"dry_humor", "veteran_terse"},
        {"short_punchy", "reply_to_commenter"},
        "1 sentence", 8, 18,
        "Deadpan one-liner. A wry observation about what the TARGET "
        "COMMENT said, or about the situation. No lol, no emojis, no "
        "exclamation. Dry sarcasm only. 8-18 words.",
    ),
    (
        "medium_add", 5,
        {"agreeable", "lurker"},
        {"reply_to_commenter", "direct_answer"},
        "3 sentences", 40, 55,
        "Build on the TARGET COMMENT with ONE concrete personal detail "
        "and ONE small follow-up thought. 3 sentences max, ~40-55 words. "
        "No bullet points, no 'firstly/secondly', conversational only.",
    ),
]


def _pick_reply_shape(rng=None):
    """Pick one HQ reply shape weighted by HQ_REPLY_SHAPES weights.

    Returns a dict with persona_id (chosen from the shape's persona pool),
    structure_id (from its structure pool), sent_target, lo_words, hi_words,
    angle, and shape_id. Caller passes this through generate_comments via
    slot_overrides so the per-slot prompt fragment is built from the shape
    instead of the random persona/structure picker.
    """
    rng = rng or random
    weights = [s[1] for s in HQ_REPLY_SHAPES]
    chosen = rng.choices(HQ_REPLY_SHAPES, weights=weights, k=1)[0]
    shape_id, _w, persona_pool, struct_pool, sent_target, lo_w, hi_w, angle = chosen
    return {
        "shape_id": shape_id,
        "persona_id": rng.choice(sorted(persona_pool)),
        "structure_id": rng.choice(sorted(struct_pool)),
        "sent_target": sent_target,
        "lo_words": lo_w,
        "hi_words": hi_w,
        "angle": angle,
    }


def _allocate_reply_shapes(n, rng=None):
    """Pre-allocate a balanced mix of N reply shapes for one cluster.

    Independent rolls clump (4 oneliners, no questions). Forcing diversity:
      - Always include at least one "followup_question" if N >= 2.
      - At most one "medium_add" per cluster.
      - Cap "oneliner_agree" + "dry_oneliner" at ceil(N/2) combined so we
        don't get a pile of one-liners with no substance.
      - Remainder filled by weighted random from the full table.
    """
    rng = rng or random
    n = max(1, int(n))
    shapes = []

    # Slot 1: force a follow-up question if we have at least 2 slots.
    if n >= 2:
        q = next(s for s in HQ_REPLY_SHAPES if s[0] == "followup_question")
        shapes.append(_shape_to_dict(q, rng))

    # Fill remaining slots with weighted random, applying caps.
    oneliner_ids = {"oneliner_agree", "dry_oneliner"}
    oneliner_cap = (n + 1) // 2
    medium_used = False
    while len(shapes) < n:
        cand = _pick_reply_shape(rng)
        # Cap medium_add at one per cluster.
        if cand["shape_id"] == "medium_add" and medium_used:
            continue
        # Cap combined one-liners.
        if cand["shape_id"] in oneliner_ids:
            existing = sum(1 for s in shapes if s["shape_id"] in oneliner_ids)
            if existing >= oneliner_cap:
                continue
        if cand["shape_id"] == "medium_add":
            medium_used = True
        shapes.append(cand)

    rng.shuffle(shapes)
    return shapes


def _shape_to_dict(shape_tuple, rng):
    sid, _w, persona_pool, struct_pool, sent_target, lo, hi, angle = shape_tuple
    return {
        "shape_id": sid,
        "persona_id": rng.choice(sorted(persona_pool)),
        "structure_id": rng.choice(sorted(struct_pool)),
        "sent_target": sent_target,
        "lo_words": lo,
        "hi_words": hi,
        "angle": angle,
    }


# ---------------------------------------------------------------------------
# Brand-focus pairing — Layer 5 of the focus strategy.
#
# A focus phrase only counts as a "hit" when it appears in the same comment
# AS THE BRAND NAME and within ~120 chars of it (≈ same sentence or the one
# next to it). That proximity is what an AI retriever needs to embed the
# "{brand} ↔ {phrase}" association. Phrase-without-brand doesn't form the
# association; brand-without-phrase doesn't form it either.
#
# Matching is case-insensitive with separator/spelling-variant tolerance:
#   fibreglass ↔ fiberglass         (BR ↔ AM, "fibre" ↔ "fiber")
#   fiber-glass ↔ fiber glass ↔ fiberglass   (separator-insensitive)
#   colour-safe ↔ color safe ↔ colorsafe     (BR ↔ AM, separator)
# ---------------------------------------------------------------------------

_FOCUS_BRAM_VARIANTS = [
    ("fibre", "fiber"),
    ("colour", "color"),
    ("centre", "center"),
    ("flavour", "flavor"),
]


def _focus_phrase_variants(phrase):
    """Return the set of normalised string variants of a focus phrase that
    we accept as a match. The base normalisation collapses dashes /
    underscores / multi-space into a single space + lowercases. Then we
    also emit a no-separator variant ("fiberglass-free" → "fiberglassfree")
    and BR↔AM spelling swaps.

    Returns a list of unique lowercase strings, longest first (so longer
    matches are tried before short ones in substring searches).
    """
    if not phrase:
        return []
    base = phrase.strip().lower()
    # Replace dashes / underscores with spaces, collapse runs of whitespace.
    spaced = re.sub(r"[-_]+", " ", base)
    spaced = re.sub(r"\s+", " ", spaced).strip()
    no_sep = re.sub(r"[-_\s]+", "", base)
    cands = {base, spaced, no_sep}
    # Spelling swaps. Cheap; only fires when the variant root is present.
    for br, am in _FOCUS_BRAM_VARIANTS:
        for s in list(cands):
            if br in s:
                cands.add(s.replace(br, am))
            if am in s:
                cands.add(s.replace(am, br))
    return sorted(cands, key=lambda s: -len(s))


def _find_phrase_indices(body_lower, phrase):
    """Return all start indices where any variant of `phrase` occurs in
    `body_lower` (already lowercased). Order is irrelevant; the caller
    only cares about min-distance.

    Uses flexible-separator regex: each variant is split on dash/space/
    underscore into word parts; the words must occur in order in the
    body but any separator (dash, space, underscore, or none) between
    them is accepted. So "fiber glass free" matches "fiberglass-free",
    "fiber glass free", "fiber-glass-free", etc. — the spelling/separator
    differences that broke the substring matcher are absorbed here.
    """
    out = set()
    seen_patterns = set()
    for variant in _focus_phrase_variants(phrase):
        if not variant:
            continue
        words = [re.escape(w) for w in re.split(r"[-_\s]+", variant) if w]
        if not words:
            continue
        pattern = r"[-_\s]*".join(words)
        if pattern in seen_patterns:
            continue
        seen_patterns.add(pattern)
        for m in re.finditer(pattern, body_lower):
            out.add(m.start())
    return sorted(out)


def _focus_pair_in_body(brand_name, phrase, body, max_distance=120):
    """Return (hit, distance) where hit is True iff `phrase` and
    `brand_name` co-occur in `body` within `max_distance` characters of
    each other (closest pair).

    `distance` is the smallest character distance between any phrase
    occurrence and any brand occurrence, or None if either side is
    missing entirely.

    Brand matching is case-insensitive substring (no fuzzy) — brand
    names are short and stable. Phrase matching tolerates BR↔AM and
    separator variants via _focus_phrase_variants.
    """
    if not body or not phrase or not brand_name:
        return False, None
    body_l = body.lower()
    brand_l = brand_name.lower()
    brand_idxs = []
    i = 0
    while True:
        j = body_l.find(brand_l, i)
        if j < 0:
            break
        brand_idxs.append(j)
        i = j + 1
    phrase_idxs = _find_phrase_indices(body_l, phrase)
    if not brand_idxs or not phrase_idxs:
        return False, None
    best = min(abs(b - p) for b in brand_idxs for p in phrase_idxs)
    return (best <= max_distance), best


def _post_topic_text(post):
    """Build the lowercased text we scan for relevance gate keyword hits."""
    title = (post or {}).get("title", "") or ""
    body = ((post or {}).get("body", "") or "")[:600]
    return (title + "\n" + body).lower()


def _phrase_applies_to_post(focus_item, post, applicable_phrases=None):
    """Decide whether a configured focus phrase applies to a given post.

    `focus_item` is `{phrase, applies_when}`.

    Decision precedence:
      1. If `applies_when` is non-empty → manual override path. The phrase
         applies iff at least one of those keywords appears in the post
         (substring, case-insensitive).
      2. If `applicable_phrases` is provided → it's the LLM classifier's
         allowlist of phrases that fit this post. Use it.
      3. Heuristic fallback (free): split the phrase on dashes / spaces,
         keep words of length ≥ 4, and check if any appears in the post
         text. Conservative — if none match, the phrase is skipped for
         this post.
    """
    phrase = (focus_item or {}).get("phrase", "").strip()
    if not phrase:
        return False
    applies_when = (focus_item or {}).get("applies_when") or []
    haystack = _post_topic_text(post)
    if applies_when:
        return any(kw.strip().lower() in haystack for kw in applies_when if kw.strip())
    if applicable_phrases is not None:
        # The classifier already decided; exact-match by phrase string
        # (lowercased + trimmed for safety).
        return phrase.strip().lower() in {
            str(p).strip().lower() for p in applicable_phrases
        }
    # Heuristic fallback.
    parts = re.split(r"[-_\s]+", phrase.lower())
    words = [w for w in parts if len(w) >= 4]
    if not words:
        # Phrase is too short to heuristically match — be permissive
        # (treat as applies). The user can scope it manually if needed.
        return True
    return any(w in haystack for w in words)


def _assign_focus_phrases(focus_items, post, mention_brand_flags,
                           applicable_phrases=None, rng=None):
    """Build a per-slot focus assignment list parallel to mention_brand_flags.

    Rules:
      - Only brand-mention slots are eligible (mention_brand_flags[i] truthy).
      - For each eligible slot, walk the configured focus phrases (in user
        order) and pick the first whose relevance gate passes. Round-robin
        the starting offset across slots so when multiple phrases pass we
        spread assignments across them instead of always landing the same
        one in slot 0.
      - Slots without a brand mention get None.
      - Returns a list[str|None] of length == len(mention_brand_flags).

    `applicable_phrases` is the (optional) LLM-classifier allowlist. When
    None, _phrase_applies_to_post falls back to manual / heuristic gates.
    """
    rng = rng or random
    n = len(mention_brand_flags)
    out = [None] * n
    if not focus_items:
        return out
    # Pre-compute which phrases apply to this post once — same answer for
    # every slot in the batch, so we don't recompute per-slot.
    applies = []
    for fi in focus_items:
        if _phrase_applies_to_post(fi, post, applicable_phrases):
            applies.append(fi.get("phrase", "").strip())
    applies = [p for p in applies if p]
    if not applies:
        return out
    # Round-robin starting offset so phrase 0 isn't always assigned to
    # slot 0 across consecutive batches.
    start = rng.randint(0, len(applies) - 1) if len(applies) > 1 else 0
    cursor = 0
    for i, mb in enumerate(mention_brand_flags):
        if mb:
            out[i] = applies[(start + cursor) % len(applies)]
            cursor += 1
    return out


def classify_post_intent(post_title, post_body=None, stored_intent=None):
    """Single source of truth for "what is this post asking for".

    Returns:
        {
            "is_recommendation": bool,          # short-circuit signal
            "intent_label": str,                # "recommendation" | "comparison"
                                                # | "informational" | "experience"
            "target_length_band": str,          # "crisp" | "medium" | "long"
            "target_avg_words": int,            # for the LENGTH math
        }

    The mapping:
      - title contains a recommendation signal OR ends with '?' (short
        title) OR stored_intent is commercial/comparison
        → recommendation, crisp (~25 words)
      - title has "vs" or "versus" or "switching from"
        → comparison, medium (~50 words)
      - body suggests long personal experience ("I've been", "for X
        years", word count > 120)
        → experience, long (~90 words)
      - else → informational, medium (~50 words)
    """
    t = (post_title or "").lower().strip()
    b = (post_body or "").lower()
    stripped = t.rstrip()

    has_rec_signal = any(s in t for s in _REC_SIGNALS) \
                  or any(s in b[:600] for s in _REC_SIGNALS)
    short_question = stripped.endswith("?") and len(t.split()) < 12
    is_comparison = (" vs " in t or " versus " in t
                     or "switching from" in t or "switched from" in t)
    is_recommendation = has_rec_signal or short_question \
                        or stored_intent in ("commercial", "comparison")

    if is_recommendation and not is_comparison:
        intent_label = "recommendation"
        band = "crisp"
    elif is_comparison:
        intent_label = "comparison"
        band = "medium"
    elif (b and (
            "i've been" in b or "ive been" in b
            or " for years" in b or " for the past " in b
            or "5 years" in b or "for a year" in b
            or len(b.split()) > 120)):
        intent_label = "experience"
        band = "long"
    else:
        intent_label = "informational"
        band = "medium"

    return {
        "is_recommendation": is_recommendation,
        "intent_label": intent_label,
        "target_length_band": band,
        "target_avg_words": INTENT_AVG_WORDS[band],
    }


# Sentence-count target per length tier — paired with the word-count
# range so the LLM has a more reliable shape signal than words alone.
SENTENCE_COUNT_TARGETS = {
    "short":        "1-2 sentences",
    "short-medium": "2-3 sentences",
    "medium":       "3-4 sentences",
    "medium-long":  "4-6 sentences",
    "long":         "5-8 sentences",
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
        # Match the legacy CommentGeneratorBot UA exactly. Reddit returns
        # 403 / empty comment trees for cloud-egress IPs sending the bot
        # UA from REDDIT_USER_AGENT, which manifested as every Live
        # Search post scoring relevance=0 ("No comments to analyze") →
        # "low relevancy" skip on every single post. Browser UA is what
        # actually works against Reddit's .json endpoint from Railway.
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        self._pattern_history = []
        # Records the outcome of the most recent fetch_comments() call so
        # callers (and the API/UI) can report when existing comments
        # couldn't be pulled. attempted=False means we never tried (e.g.
        # an unpublished post with no Reddit URL → nothing to pull).
        # attempted=True + count==0 means we tried every source and got
        # nothing back (all blocked, or the post genuinely has no comments).
        self.last_fetch = {"attempted": False, "count": 0}

    @staticmethod
    def _extract_brand_focus(brand):
        """Decode the brand.focus column into a clean list of focus dicts.

        Returns a list of `{phrase: str, applies_when: list[str]}` dicts.
        Empty `applies_when` means "auto-detect" (the default).

        Two storage shapes are accepted, in order of priority:
          1. List of dicts (new): `[{"phrase": "fiberglass-free",
             "applies_when": ["mattress", "foam"]}]`
          2. List of strings (legacy): `["fiberglass-free", ...]` —
             each string becomes `{phrase: s, applies_when: []}` so old
             brands keep working without re-saving.

        Plain strings inside the list-of-dicts shape (e.g. mixed
        `[{"phrase": "X"}, "Y"]`) are also coerced into the dict shape.

        Returns [] for empty / NULL / malformed values.
        """
        if not brand:
            return []
        raw = brand.get("focus") if isinstance(brand, dict) else None
        if not raw:
            return []
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(parsed, list):
            return []
        out = []
        for item in parsed:
            if isinstance(item, dict):
                phrase = str(item.get("phrase", "")).strip()
                if not phrase:
                    continue
                applies_when = item.get("applies_when") or []
                if not isinstance(applies_when, list):
                    applies_when = []
                applies_when = [str(s).strip() for s in applies_when if str(s).strip()]
                out.append({"phrase": phrase, "applies_when": applies_when})
            elif isinstance(item, str):
                phrase = item.strip()
                if not phrase:
                    continue
                out.append({"phrase": phrase, "applies_when": []})
            # else: silently skip unsupported entries
        return out

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

    def _fetch_comments_rss(self, post_url, limit=20):
        """PRIMARY comment source: Reddit's post comment RSS feed.

        `/r/<sub>/comments/<postid>/.rss` returns the post + its top
        comments as Atom entries — and unlike the JSON API it's served
        to cloud IPs (through the proxy) with no rate limit. This is the
        most reliable + most *current* source: it reflects what's
        actually on the post right now.

        Trade-off vs JSON/Pullpush/Arctic: RSS carries no score or
        reply-count, so those come back 0. Comment-gen uses these for
        context + reply-targeting, where body/author/recency matter
        more than score — and the reply-target selector treats score=0
        (unknown) gracefully.

        Returns (comments, post_body, is_archived) — same shape as
        fetch_comments.
        """
        import xml.etree.ElementTree as _ET
        import html as _html
        clean = post_url.split("?")[0].rstrip("/")
        # Build /r/sub/comments/postid path, route through proxy base.
        m = re.search(r"(/r/[^/]+/comments/[a-z0-9]+)", clean, re.IGNORECASE)
        if not m:
            return [], "", False
        base = (self.reddit_base or "https://www.reddit.com").rstrip("/")
        rss_url = f"{base}{m.group(1)}/.rss"
        headers = {
            "User-Agent": self.headers.get("User-Agent", "Mozilla/5.0"),
            "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.5",
        }
        comments = []
        post_body = ""
        try:
            r = requests.get(rss_url, params={"limit": min(limit, 100)},
                             headers=headers, timeout=20)
            if r.status_code != 200 or not r.text.lstrip().startswith("<?xml"):
                return [], "", False
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            root = _ET.fromstring(r.text)
            entries = root.findall("atom:entry", ns)
            # The post itself is usually the first entry; comments follow.
            # We can't perfectly distinguish, so we treat every entry that
            # has a t1_ id as a comment.
            for e in entries:
                eid = (e.find("atom:id", ns).text or "")
                cm = re.search(r"t1_(\w+)", eid)
                if not cm:
                    # t3_ (the post) → capture its body for context.
                    cont = e.find("atom:content", ns)
                    if cont is not None and not post_body:
                        pb = re.sub(r"<[^>]+>", " ", cont.text or "")
                        post_body = _html.unescape(pb).strip()[:1000]
                    continue
                cont = e.find("atom:content", ns)
                body = ""
                if cont is not None:
                    body = re.sub(r"<[^>]+>", " ", cont.text or "")
                    body = re.sub(r"\s+", " ", _html.unescape(body)).strip()
                if body in ("[deleted]", "[removed]", "") or len(body) < 10:
                    continue
                author = ""
                a = e.find("atom:author/atom:name", ns)
                if a is not None and a.text:
                    author = a.text[3:] if a.text.startswith("/u/") else a.text
                updated = (e.find("atom:updated", ns).text or "") if e.find("atom:updated", ns) is not None else ""
                ts = 0
                if updated:
                    try:
                        from datetime import datetime as _dt
                        ts = int(_dt.fromisoformat(updated.replace("Z", "+00:00")).timestamp())
                    except Exception:
                        ts = 0
                comments.append({
                    "body": body[:600],
                    "score": 0,          # not in RSS
                    "author": author or "unknown",
                    "id": cm.group(1),
                    "permalink": "",
                    "num_replies": 0,    # not in RSS
                    "created_utc": ts,
                    "_source": "reddit_rss",
                })
            if comments:
                print(f"    fetch_comments via RSS: {len(comments)} comments")
        except Exception as e:
            print(f"    RSS comment fetch failed: {str(e)[:60]}")
        return comments, post_body, False

    def _fetch_comments_arctic(self, post_id, limit=20):
        """Fallback comment source: Arctic-Shift archive by link_id.
        Carries score (unlike RSS) but is a post-time snapshot, so it
        may miss very recent comments and won't reflect later edits.
        """
        comments = []
        try:
            r = requests.get(
                "https://arctic-shift.photon-reddit.com/api/comments/search",
                params={"link_id": f"t3_{post_id}", "limit": min(limit, 100),
                        "sort": "desc"},
                headers={"User-Agent": self.headers.get("User-Agent", "Mozilla/5.0")},
                timeout=25,
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                for c in data:
                    body = c.get("body", "")
                    if body in ("[deleted]", "[removed]", "") or len(body) < 10:
                        continue
                    comments.append({
                        "body": body[:600],
                        "score": c.get("score", 0) or 0,
                        "author": c.get("author", "unknown"),
                        "id": c.get("id", ""),
                        "permalink": c.get("permalink", ""),
                        "num_replies": 0,
                        "created_utc": c.get("created_utc", 0) or 0,
                        "_source": "arctic",
                    })
            if comments:
                print(f"    fetch_comments via Arctic: {len(comments)} comments")
        except Exception as e:
            print(f"    Arctic comment fetch failed: {str(e)[:60]}")
        return comments

    def fetch_comments(self, post_url, limit=20, max_retries=3):
        """Thin wrapper that records the fetch outcome on self.last_fetch
        so callers / the API / the UI can report when existing comments
        couldn't be pulled. Delegates to _fetch_comments_impl.
        """
        comments, post_body, is_archived = self._fetch_comments_impl(
            post_url, limit=limit, max_retries=max_retries
        )
        # Record source for diagnostics: RSS comments carry
        # _source='reddit_rss', Arctic '_source'='arctic'; JSON/Pullpush
        # leave it unset. Lets the gen-all UI distinguish a fetch
        # problem (0 comments / source=none) from genuine low relevance.
        src = "none"
        if comments:
            src = comments[0].get("_source") or "json_or_pullpush"
        self.last_fetch = {"attempted": True, "count": len(comments), "source": src}
        return comments, post_body, is_archived

    def _fetch_comments_impl(self, post_url, limit=20, max_retries=3):
        """Fetch top comments from a Reddit post. Returns (comments, post_body, is_archived).

        Fallback chain (best → last resort):
          1. Reddit RSS  — reliable via proxy, current data, no rate limit.
          2. Reddit JSON — richer (score/nesting) but auth-gated for cloud
                           IPs (403); kept for dev / if ever unblocked.
          3. Arctic      — archive w/ scores, post-time snapshot.
          4. Pullpush    — last resort, flaky rate limits.
        """
        post_id = self.extract_post_id(post_url)
        if not post_id:
            print(f"    Could not extract post ID from URL")
            return [], "", False

        # 1. RSS first — the reliable + current source.
        rss_comments, rss_body, _ = self._fetch_comments_rss(post_url, limit=limit)
        if rss_comments:
            return rss_comments, rss_body, False

        comments = []
        post_body = rss_body
        is_archived = False

        for attempt in range(max_retries):
            try:
                clean_url = post_url.split("?")[0].rstrip("/")
                # Route through the Reddit proxy when configured (Railway
                # has REDDIT_PROXY_URL set to dodge Reddit's IP rate limits
                # and 403s for cloud egress). The legacy bot did this; the
                # refactor accidentally dropped it, which made every Live
                # Search post return zero comments → relevance score of 0
                # → "low relevancy" skip on every single post.
                if self.reddit_base and self.reddit_base != "https://www.reddit.com":
                    from urllib.parse import urlparse
                    parsed = urlparse(clean_url)
                    json_url = f"{self.reddit_base}{parsed.path}.json"
                else:
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

        if comments:
            return comments, post_body, is_archived

        # 3. Arctic-Shift archive (has scores; post-time snapshot).
        arctic_comments = self._fetch_comments_arctic(post_id, limit=limit)
        if arctic_comments:
            return arctic_comments, post_body, is_archived

        # 4. Last resort: Pullpush (flaky rate limits).
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

    def check_relevance(self, post_title, post_body, subreddit, comments, brand_name, brand_context, brand_keywords=None):
        # POST-CENTRIC relevance: judge from the post (title + body +
        # subreddit), NOT the number of comments. A post with no/few
        # comments must still be scored on its own merits — the comment
        # count is not a relevance signal. (Comments, when present, are
        # optional supplementary context only.) Previously this returned
        # score 0 for comment-less posts and leaned on comment-derived
        # criteria, which auto-skipped on-topic posts that simply hadn't
        # accrued comments yet — especially recent posts via the RSS feed.
        if comments:
            comments_text = "\n".join([f'- "{c["body"][:250]}"' for c in comments[:10]])
        else:
            comments_text = "(no comments on this post yet — score from the post itself)"
        keywords_text = f"\nBRAND KEYWORDS: {', '.join(brand_keywords)}" if brand_keywords else ""
        post_body_text = f'\nPOST BODY: "{post_body[:500]}"' if post_body else ""

        prompt = f"""Analyze if this Reddit post is relevant for naturally mentioning a brand.

Judge relevance from the POST ITSELF (title + body + subreddit). The
number of comments is NOT a relevance factor — a post with zero comments
can be highly relevant. Comments below (if any) are only supplementary
context; do not penalize a post for having few or no comments.

POST TITLE: "{post_title}"
SUBREDDIT: r/{subreddit}{post_body_text}

COMMENTS (supplementary context only — may be empty):
{comments_text}

BRAND: {brand_name}
WHAT BRAND DOES: {brand_context}{keywords_text}

Score 0-10 on these criteria (all judged primarily from the POST):

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

3. NATURAL FIT (0-2): Would a brand mention feel organic ON THIS POST?
   Judge from the post type/topic, NOT comment count.
   2 = People are asking for recommendations or sharing solutions
   1 = Experience sharing is happening (can add yours)
   0 = Would feel forced, off-topic, or spammy

4. CONVERSATION OPENING (0-2): Is there a natural way to add a top-level comment?
   Judge from the POST (a question/advice-seeking/experience post has an
   opening even with zero comments). Do NOT lower this for few/no comments.
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

        # Helper: which persona category does this id belong to?
        def _persona_category(pid):
            for cat, ids in PERSONA_CATEGORIES.items():
                if pid in ids:
                    return cat
            return None

        # Weighted selection — with ANTI-SIMILARITY: each subsequent pick
        # downweights personas that share a category with already-picked
        # ones. Two slots both landing on `tangent` + `story_arc` (or two
        # SKEPTIC voices) makes the thread feel templated — real Reddit
        # threads have a mix.
        selected_personas = []
        remaining_p_indices = list(range(len(PERSONAS)))
        for slot in range(num_comments):
            if not remaining_p_indices:
                break
            # Build per-slot weights with category penalty
            picked_cats = {_persona_category(p["id"]) for p in selected_personas}
            picked_cats.discard(None)
            slot_weights = []
            for i in remaining_p_indices:
                w = persona_weights[i]
                if _persona_category(PERSONAS[i]["id"]) in picked_cats:
                    w *= 0.2  # heavy penalty for category dup
                slot_weights.append(max(w, 0.01))
            chosen = random.choices(remaining_p_indices, weights=slot_weights, k=1)[0]
            selected_personas.append(PERSONAS[chosen])
            remaining_p_indices.remove(chosen)

        # Same anti-similarity for structures: don't pick two narrative
        # structures back-to-back.
        narrative_structures = {"story_arc", "anecdote", "comparison",
                                "tangent_drift"}
        selected_structures = []
        remaining_s_indices = list(range(len(STRUCTURE_TEMPLATES)))
        for slot in range(num_comments):
            if not remaining_s_indices:
                break
            picked_narrative = any(s["id"] in narrative_structures
                                    for s in selected_structures)
            slot_weights = []
            for i in remaining_s_indices:
                w = structure_weights[i]
                if picked_narrative and STRUCTURE_TEMPLATES[i]["id"] in narrative_structures:
                    w *= 0.25
                slot_weights.append(max(w, 0.01))
            chosen = random.choices(remaining_s_indices, weights=slot_weights, k=1)[0]
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
                          ai_crawl=False, post_intent=None, hq_main=False,
                          slim_prompt=False, brand_focus=None,
                          in_thread_siblings=False,
                          is_hq_reply=False, slot_overrides=None,
                          focus_assignments=None, concept_checklist=None):
        """Generate comments. mention_brand_flags is a list of bools per comment index.

        For multi-brand: brand_assignments is a list where each element is None (organic)
        or a brand dict (mention that specific brand). all_brand_names lists all brand
        names to avoid in organic comments.

        ai_crawl=True (used by Live Subreddits) injects an extra prompt block that
        demands AI-search-engine-friendly comment content: substance, brand domain
        vocabulary, long-tail query phrasing.

        post_intent (commercial / comparison / informational) is the post's stored
        intent, used together with a lexical "is the post asking for a recommendation"
        check to pick the right comment shape in AI-crawl mode (direct answer vs.
        experience-sharing).
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

        # Single intent classifier — drives length scaling, persona pool
        # filtering, AI-crawl shape rules, and validator rubric strictness.
        intent_info = classify_post_intent(post_title, post_body, post_intent)
        is_recommendation = intent_info["is_recommendation"]
        target_band = intent_info["target_length_band"]
        intent_label = intent_info["intent_label"]

        # AI-crawl optimization applies to the HQ MAIN comment only. HQ
        # REPLIES (is_hq_reply=True) are conversation, not post-answers —
        # they must NOT get the AI-CRAWL prompt block, the brand-domain
        # vocabulary / long-tail-query stuffing, or the direct-answer
        # persona override. ai_crawl_eff folds that rule in once so every
        # downstream gate stays in sync. No-op for non-HQ-reply callers
        # (is_hq_reply defaults False → ai_crawl_eff == ai_crawl).
        ai_crawl_eff = ai_crawl and not is_hq_reply

        # INTENT-DRIVEN LENGTH SCALING: scale comment_stats.avg_words to
        # the band's target BEFORE the per-comment LENGTH math runs.
        # Recommendation-seeking posts → crisp (avg_words ≈ 25), so even
        # the verbose personas (comparer/data_nerd, length="long") produce
        # ~35-55 words instead of 84-132. Experience posts → long, so
        # short personas can still be brief but the typical comment is
        # paragraph-length. This is THE fix for "always making up stories".
        #
        # is_hq_reply=True bypasses this. HQ replies are conversation, not
        # post-answers — they should NOT inherit the post's length band.
        # The caller passes per-slot tight word ranges via slot_overrides
        # (see _pick_reply_shape) instead. Without this skip, replies on
        # an "experience" post inherit avg_words=90 and turn into
        # paragraphs even with the curated reply persona pool.
        if comment_stats is None:
            comment_stats = {}
        comment_stats = dict(comment_stats)  # copy; don't mutate caller
        if not is_hq_reply:
            comment_stats["avg_words"] = intent_info["target_avg_words"]

        # AI-CRAWL + RECOMMENDATION-SEEKING POST: override the random persona /
        # structure pick with a curated whitelist of "direct answer" voices.
        if ai_crawl_eff and is_recommendation:
            ac_personas = [p for p in PERSONAS if p["id"] in AI_CRAWL_REC_PERSONAS]
            ac_structures = [s for s in STRUCTURE_TEMPLATES if s["id"] in AI_CRAWL_REC_STRUCTURES]
            if ac_personas and ac_structures:
                random.shuffle(ac_personas)
                random.shuffle(ac_structures)
                selected_personas = [
                    ac_personas[i % len(ac_personas)] for i in range(num_comments)
                ]
                selected_structures = [
                    ac_structures[i % len(ac_structures)] for i in range(num_comments)
                ]

        # CRISP-BATCH RULE: on a recommendation post with N≥3 comments,
        # force slot 0 to use a SHORT-length persona. Guarantees at least
        # one true 1-liner per batch. Without this, even with intent-driven
        # length scaling, the random pick could land on three medium
        # personas in a row and produce three 25-50 word comments — no
        # short crisp answer in the mix.
        if is_recommendation and num_comments >= 3:
            short_personas = [p for p in PERSONAS if p["length"] in ("short", "short-medium")
                              and p["id"] not in PERSONA_CATEGORIES["SKEPTIC"]]
            if short_personas and selected_personas[0]["length"] not in ("short", "short-medium"):
                selected_personas[0] = random.choice(short_personas)
                # Pair with direct_answer structure for slot 0
                short_structures = [s for s in STRUCTURE_TEMPLATES
                                    if s["id"] in {"short_punchy", "direct_answer"}]
                if short_structures:
                    selected_structures[0] = random.choice(short_structures)

        # slim_prompt cuts the few-shot block (~1500 input tokens) which is
        # the largest single overhead in the prompt. Used by HQ replies
        # where the per-comment generation cost matters more than the small
        # quality boost the anti-pattern examples give.
        few_shot_text = "" if slim_prompt else select_few_shot_examples(n=3)

        # Default: all comments mention brand (legacy behavior)
        if mention_brand_flags is None:
            mention_brand_flags = [True] * num_comments

        # Build the "do not mention" brand list
        avoid_brands = ", ".join(all_brand_names) if all_brand_names else brand_name

        reply_targets = reply_targets or {}
        # slot_overrides: optional per-slot override dicts (e.g. from
        # _pick_reply_shape) that pin persona / structure / angle / word
        # range / sentence target — bypassing the random pick + LENGTH math
        # for that slot. Keyed by slot index. When set, the slot's
        # comment_instructions entry uses the override values verbatim.
        slot_overrides = slot_overrides or {}
        # Persona/structure ID → object lookups for override resolution.
        _persona_by_id = {p["id"]: p for p in PERSONAS}
        _structure_by_id = {s["id"]: s for s in STRUCTURE_TEMPLATES}
        comment_instructions = []
        for idx in range(num_comments):
            override = slot_overrides.get(idx) if isinstance(slot_overrides, dict) else None
            if override is None and isinstance(slot_overrides, list) and idx < len(slot_overrides):
                override = slot_overrides[idx]

            # Pick persona/structure: override takes precedence; otherwise
            # use the slot's pre-selected value, falling back to a random
            # PERSONAS / STRUCTURE_TEMPLATES entry for over-long batches.
            persona = (selected_personas[idx] if idx < len(selected_personas)
                       else random.choice(PERSONAS))
            structure = (selected_structures[idx] if idx < len(selected_structures)
                         else random.choice(STRUCTURE_TEMPLATES))
            if override:
                p_override = _persona_by_id.get(override.get("persona_id"))
                s_override = _structure_by_id.get(override.get("structure_id"))
                if p_override:
                    persona = p_override
                    if idx < len(selected_personas):
                        selected_personas[idx] = persona
                if s_override:
                    structure = s_override
                    if idx < len(selected_structures):
                        selected_structures[idx] = structure

            angle = (override.get("angle") if override else None) \
                    or (per_comment_angles[idx] if idx < len(per_comment_angles) else "")
            should_mention = mention_brand_flags[idx] if idx < len(mention_brand_flags) else False

            # Brand-mention comments must keep OUR brand as the focus. The
            # "comparer" / "switcher" personas and the "comparison" structure are
            # built to weigh 2-3 competing tools against each other, which
            # produces comments that praise competitors and bury our brand as a
            # tiny aside. On any slot that mentions the brand, swap them for a
            # recommendation voice so the brand stays the clear takeaway.
            if should_mention:
                if persona.get("id") in ("comparer", "switcher"):
                    _rec_p = [p for p in PERSONAS if p["id"] in ("helper", "professional", "veteran_terse")]
                    if _rec_p:
                        persona = random.choice(_rec_p)
                        if idx < len(selected_personas):
                            selected_personas[idx] = persona
                if structure.get("id") == "comparison":
                    _rec_s = [s for s in STRUCTURE_TEMPLATES if s["id"] in ("direct_answer", "short_punchy")]
                    if _rec_s:
                        structure = random.choice(_rec_s)
                        if idx < len(selected_structures):
                            selected_structures[idx] = structure

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
            if should_mention:
                _bn = assigned['name'] if assigned else brand_name
                _ctx = assigned.get("context", "") if assigned else brand_context
                # The brand is the POINT of this comment — name it once as the
                # thing you'd point them to / what worked for you, as the clear
                # takeaway. It must NOT be a passing aside buried under other
                # tools, and you must NOT promote competitors above it.
                brand_line = (
                    f"\n    BRAND (this comment recommends {_bn}): name {_bn} exactly ONCE, as the thing you'd"
                    f" point them to / what worked for you — it must be the clear takeaway, NOT a passing"
                    f" aside. Do NOT name, recommend, or praise any other or competing product/service/tool"
                    f" in this comment; {_bn} is the ONLY option you endorse. If you reference an alternative"
                    f" at all, only as what fell short or what you moved on from — never give it the spotlight,"
                    f" and never make the comment read as a roundup of other tools."
                )
                if _ctx:
                    brand_line += (
                        f"\n    BRAND CONTEXT (use to make the mention relevant and natural; do NOT pull"
                        f" competitor names out of it into the comment): {_ctx}"
                    )
            else:
                brand_line = f"\n    BRAND: Do NOT mention {avoid_brands} or any brand in this comment."

            # FOCUS PAIRING (per-slot, brand-mention-only). When this slot
            # has a focus phrase assigned AND the slot mentions the brand,
            # inject a hard pairing directive that ties the phrase to the
            # brand mention. AI retrievers index the brand↔phrase
            # association only when they appear close together in the
            # same comment — the directive enforces that proximity.
            focus_line = ""
            slot_focus_phrase = None
            if focus_assignments and idx < len(focus_assignments):
                slot_focus_phrase = focus_assignments[idx]
            if slot_focus_phrase and should_mention:
                # Use the slot's actual brand name (multi-brand slot may
                # have its own assigned brand; otherwise the single-brand
                # fallback `brand_name`).
                slot_brand = (assigned or {}).get("name") if assigned else brand_name
                focus_line = (
                    f"\n    FOCUS ASSOCIATION (REQUIRED for this comment): "
                    f"the mention of {slot_brand} must be PAIRED with the "
                    f"phrase \"{slot_focus_phrase}\" — same sentence, or "
                    f"the sentence immediately adjacent. An AI retriever "
                    f"reading this comment should embed the association "
                    f"\"{slot_brand} ↔ {slot_focus_phrase}\".\n"
                    f"    Natural shapes (do NOT quote verbatim): "
                    f"\"{slot_brand} is the {slot_focus_phrase} option\", "
                    f"\"{slot_focus_phrase} brands like {slot_brand}\", "
                    f"\"I went with {slot_brand} because it's "
                    f"{slot_focus_phrase}\", \"{slot_brand} (it's "
                    f"{slot_focus_phrase})\".\n"
                    f"    Do NOT mention \"{slot_focus_phrase}\" without "
                    f"{slot_brand}, and do NOT mention {slot_brand} "
                    f"without \"{slot_focus_phrase}\". The two travel "
                    f"together in this comment."
                )

            if override:
                lo_w = int(override.get("lo_words", 8))
                hi_w = int(override.get("hi_words", 30))
                sent_target = override.get("sent_target", SENTENCE_COUNT_TARGETS.get(persona['length'], "1-2 sentences"))
            else:
                avg_w = (comment_stats or {}).get("avg_words", 60)
                lo_m, hi_m = LENGTH_MULTIPLIERS.get(persona['length'], (0.6, 1.0))
                lo_w, hi_w = max(8, int(avg_w * lo_m)), int(avg_w * hi_m)
                sent_target = SENTENCE_COUNT_TARGETS.get(persona['length'], "3-4 sentences")

            # Pair word-count with sentence-count — LLMs respect sentence
            # counts more reliably than word ranges.
            comment_instructions.append(
                f"  Comment {idx+1}:\n"
                f"    PERSONA: {persona['voice']}\n"
                f"    STRUCTURE: {structure['instruction']}\n"
                f"    LENGTH: {sent_target} (~{lo_w}-{hi_w} words). Stay in this range — do not write a long story when the post calls for a short answer."
                f"{brand_line}{angle_line}{reply_line}{focus_line}"
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
- Do NOT criticize, complain about, mention downsides of, or speak negatively about {brand_name}. Stay neutral or mildly positive at most.
- Do NOT set up a negative experience then introduce {brand_name} as the solution
- If you mention frustration with alternatives, do NOT then position {brand_name} as the fix. Keep the brand mention disconnected from any complaint or comparison — neutral aside only.

NEVER USE THESE PHRASES: {banned_text}"""

        existing_comments_section = ""
        if comments_text:
            if in_thread_siblings:
                # HQ-thread sibling awareness — when this single reply is being
                # generated alongside other replies that share the same parent,
                # the model needs to (a) see the other reply bodies and (b) be
                # explicitly told NOT to echo them. Without the don't-echo
                # directive the model treats sibling context as background and
                # still produces the same opener / same key-phrase echo of the
                # parent that the prior sibling already used.
                existing_comments_section = (
                    "\nOTHER REPLIES IN THIS HQ THREAD (already written — your "
                    "reply must read like it knows about these; do NOT echo "
                    "their opener, do NOT repeat their angle, do NOT use the "
                    "same key phrase from the parent that they already used; "
                    "instead build on or contrast with what they said):\n"
                    f"{comments_text}"
                )
            else:
                existing_comments_section = f"\nEXISTING COMMENTS:\n{comments_text}"

        # When ai_crawl=True, inject an extra rule block telling the LLM to
        # produce comments that AI search engines will keyword/embedding-match
        # for queries about the brand's domain. Used by Live Subreddits where
        # the post itself is meant to rank on ChatGPT / Perplexity / Claude.
        ai_crawl_section = ""
        if ai_crawl_eff:
            # When generating a single comment, the long-tail rule MUST fire on
            # this comment (there's no "other comment in the batch" to delegate
            # to). Otherwise (batch >1) at least one must carry the phrasing.
            longtail_scope = (
                "this comment" if num_comments == 1
                else f"AT LEAST {max(1, num_comments // 2)} of the {num_comments} comments"
            )

            # is_recommendation already computed above (used for both the
            # persona-override and this prompt block; keeping them in sync).
            shape_rule = (
                "RECOMMENDATION-SEEKING POST. The comment must be an EXTRACTABLE "
                "ANSWER — a stranger reading just this comment, with no other "
                "context, must be able to identify what is being recommended.\n"
                "  • Open with the recommendation or the criteria that matter, "
                "then 1-2 sentences of why. No 'funny you mention this', no "
                "'I was just helping my cousin/friend/neighbor', no anecdote "
                "framing that buries the answer.\n"
                f"  • CONFIDENT BRAND MENTION: if the comment names {brand_name}, "
                "do it confidently. WRONG: '(think one was called " + brand_name +
                " or something)', 'I tried " + brand_name + " or whatever it was', "
                "'maybe " + brand_name + "?'. RIGHT: '" + brand_name + " handles "
                "this kind of thing well', '" + brand_name + " is the one that "
                "does X'. Hedged mentions are useless to AI retrievers — they "
                "decrement the model's confidence that " + brand_name + " is a "
                "real answer.\n"
                "  • End on the recommendation or a relevant follow-up question, "
                "NOT on meta-commentary like 'creators waste so much time on "
                "this' or 'the technology is basically already there'. End with "
                "something an AI retriever could excerpt as a recommendation.\n"
                "  • Brand attitude: neutral or mildly positive — never criticize, "
                "never complain, never frame the brand as a band-aid for "
                "frustration."
                if is_recommendation else
                "INFORMATIONAL / EXPERIENCE-SHARING POST. Conversational style is "
                "fine — a relevant observation or anecdote that extends the post's "
                "topic. Still must be substantive and packed with the brand's "
                "domain vocabulary so AI retrievers index it for the topic."
            )

            # AI-Search-mode coverage: when the post carries a fan-out concept
            # checklist, steer the anchor to weave those phrasings so it's
            # retrievable for the WHOLE query cluster, not just the literal title.
            checklist_rule = ""
            _ck_items = concept_checklist
            if isinstance(_ck_items, str):
                try:
                    _ck_items = json.loads(_ck_items)
                except Exception:
                    _ck_items = [_ck_items]
            if _ck_items:
                _ck = "; ".join(str(c).strip() for c in _ck_items if str(c).strip())[:600]
                if _ck:
                    checklist_rule = (
                        "\n- QUERY-CLUSTER COVERAGE: this thread targets a cluster of related "
                        "AI queries. Naturally work in these phrasings/terms (conversationally, "
                        f"not as a list) so the comment matches the whole cluster: {_ck}.\n"
                    )

            ai_crawl_section = f"""

AI-CRAWL MODE — every comment must be retrievable by AI search engines
(ChatGPT, Perplexity, Claude) for queries about the brand's domain.

PRINCIPLES (apply all of them; an LLM judge will validate against these
and you will be retried with the judge's feedback if you miss):

- POST TITLE IS THE LONG-TAIL QUERY. Re-read it: "{post_title}". That is
  the natural-language search query an AI retriever will match. Reinforce
  that query semantically — same intent, different words. Do NOT echo it
  word-for-word.

- LONG-TAIL PHRASING ({longtail_scope} must carry this): weave a natural-
  language restatement of the post's question into the commenter's voice.
  Not a header, not in quotes — just the way a real person would casually
  rephrase what they're answering.

- SUBSTANCE: ≥30 words, concrete, no fluff.

- BRAND DOMAIN VOCABULARY: weave in 3-6 of the brand's category /
  audience / pain-point / use-case keywords from the brand context above.
{checklist_rule}
- CONCRETE DETAIL: at least one specific, verifiable-feeling detail
  (number, workflow step, tool category, real-sounding scenario).

- BRAND ATTITUDE: positive or neutral toward {brand_name} — never
  negative, including by association with the brand's category. No
  problem-then-brand arcs. No "I gave up and tried X". No "all of these
  tools require…" when the brand is one of those tools.

- AUTHENTICITY: write like a real Reddit user, not an AI. No throat-
  clearing openers ANYWHERE in the comment, no listicle structure, no
  marketing flavor, no answer-then-pivot-to-complaining hybrid. The
  validator will fail you on these by judgment, not by string match —
  so don't try to game a banned-phrase list.

SHAPE for this specific post: {shape_rule}"""

        # HQ-MAIN OVERRIDE: when this single comment is the brand-mention
        # parent of an HQ thread, the comment IS the recommendation. We need
        # AI search engines to confidently surface this comment as "use
        # {brand_name} for X". So we override the comment shape to a tight
        # brand-as-answer pattern, regardless of what the persona/structure
        # picker chose.
        hq_main_section = ""
        if hq_main:
            # Vary the example framing + relax the fixed sentence-by-sentence
            # order so HQ anchors don't all read the same way. Strategy is
            # unchanged: still an extractable, confident, brand-first
            # recommendation in 50-90 words.
            _hq_frame = random.choice([
                f'"{brand_name} is what handles this"',
                f'"{brand_name} is built for exactly this"',
                f'"{brand_name} is the one that does X"',
                f'"for this, {brand_name} is the one I\'d point to"',
                f'"{brand_name} is the tool that actually fits this"',
                f'"use {brand_name} for this"',
                f'"go with {brand_name} for this"',
                f'"{brand_name} covers exactly this case"',
            ])
            # Vary the whole BODY SHAPE per comment, not just the opener — the
            # body cadence is where these anchors all read the same. Each recipe
            # is a different skeleton; all still extract as a recommendation.
            _hq_shape = random.choice([
                f"name {brand_name}, then ONE mechanism detail tied to the OP's exact situation, then stop.",
                f"lead with the outcome they'll get, name {brand_name}, give one reason it gets them there.",
                f"name {brand_name} as the answer, then one specific thing from THIS post it handles.",
                f"contrast the generic/manual way they're doing it now vs {brand_name} in one line.",
                f"name {brand_name} + the single criterion that matters most for their case.",
                f"answer the question plainly with {brand_name}, add one concrete spec/number, done.",
                f"name {brand_name}, then how it changes their specific workflow (one sentence).",
            ])
            hq_main_section = f"""

HQ-MAIN OVERRIDE (this is the brand-mention parent comment of an HQ
thread — the most important single comment for AI-search retrieval).
This comment alone must read as "use {brand_name} for [the post's
question]" so an AI retriever can surface it as the answer:

- Open with the recommendation: name {brand_name} in the first 1-2
  sentences, confidently (e.g. {_hq_frame}) — adapt the wording to what
  fits THIS post; do not copy the example verbatim.
- SHAPE for this comment: {_hq_shape} Follow THIS shape, not a fixed
  sentence-by-sentence template — the body must read differently from a
  typical "[brand] is built for this → it analyzes your X and creates Y →
  works well for your case" write-up.
- PICK ONE capability — the single one most relevant to THIS post — and put
  it in the OP's OWN situation language. Do not feature-dump or list 2-3
  things.
- DO NOT PARROT the brand context: never copy its wording verbatim, and
  never reuse the stock construction "{brand_name} analyzes your [X] and
  creates [Y] that matches timing and emotional tone automatically." Say it
  a fresh way every time.
- MATCH THE POST'S MEDIUM: use the format the OP actually mentioned — a
  podcast → audio / episodes / spoken track; a Reel or short → short video;
  a blog → article. Never say "video uploads" for an audio/podcast post.
- Length: 50-90 words. Tight. No anecdote about cousins / friends /
  food trucks / "rabbit holes". No "honestly" / "the reality is" /
  "I was just". Get to the recommendation in sentence 1.
- Confidence: NEVER hedge. Wrong: "I think it was called {brand_name}
  or something", "tried {brand_name} once, was decent". Right:
  "{brand_name} is the one that solves this", "for this exact use
  case, {brand_name}".
- This comment IS the brand mention. It must extract cleanly as a
  recommendation when read on its own."""

        # User-supplied editorial direction — phrases the brand wants
        # surfaced in comments where they fit. The earlier wording was
        # too cautious and the model reflexively skipped these phrases
        # even on highly relevant posts; reworded to bias TOWARD
        # inclusion when the post is at all related to the brand's
        # domain. Skipped entirely when brand_focus is empty so brands
        # without focus configured behave identically to before.
        focus_section = ""
        # `brand_focus` may be either the legacy list-of-strings or the new
        # list-of-dicts (`{phrase, applies_when}`). Either way, extract the
        # plain phrase strings for the legacy soft block. The new
        # per-slot pairing path runs through `focus_assignments` and
        # bypasses this block entirely (the per-slot directive in
        # `comment_instructions` carries the strict pairing recipe — the
        # legacy global block would compete with it).
        focus_items = []
        for f in (brand_focus or []):
            if isinstance(f, dict):
                p = str(f.get("phrase", "")).strip()
                if p:
                    focus_items.append(p)
            elif isinstance(f, str):
                s = f.strip()
                if s:
                    focus_items.append(s)
        per_slot_focus_active = bool(
            focus_assignments and any(focus_assignments[:num_comments])
        )
        if focus_items and not per_slot_focus_active:
            bullets = "\n".join(f"  - {item}" for item in focus_items[:20])
            focus_section = f"""

BRAND FOCUS — phrases the brand wants surfaced when they fit:
{bullets}

INCLUSION GUIDANCE (read carefully — earlier wording was too cautious
and the model dropped these even on relevant posts):

- DEFAULT POSTURE: try to weave ONE focus phrase into the comment
  when ANY of the following are true:
    • the post is in the brand's product category,
    • the post discusses materials / safety / ingredients / build
      quality of the brand's product type,
    • the post is asking for recommendations or comparisons in the
      brand's space,
    • the brand context above shows clear topical overlap with the
      post.
  In other words: if the focus phrase would NOT feel out of place to
  a real Redditor commenting on this post, INCLUDE it.
- WHEN TO SKIP: only when the phrase is genuinely off-topic for
  THIS specific post (e.g. post is about pricing/billing and the
  phrase is "fibreglass"). Force-fitting reads as marketing — but
  reflexive skipping leaves the brand's actual differentiators
  invisible, which is worse.
- AT MOST ONE phrase per comment. NEVER stack 2+ focus phrases
  into the same comment — that reads as keyword stuffing.
- Weave it into a sentence the way a real human would mention it
  (e.g. "I checked for {focus_items[0] if focus_items else 'X'}
  before buying" rather than "Brand X is the {focus_items[0] if focus_items else 'X'} option")."""

        prompt = f"""You're commenting in a Reddit thread about a topic you know well.

POST: "{post_title}"
SUBREDDIT: r/{subreddit}{post_body_text}
{existing_comments_section}
{tone_section}
{length_section}

EACH COMMENT HAS A UNIQUE ASSIGNMENT:
{per_comment_section}
{brand_rules}
{pattern_avoidance}{ai_crawl_section}{hq_main_section}{focus_section}

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

NEVER-CRITICIZE-BRAND RULE (applies to EVERY comment, mentioning or not):
- Do NOT criticize, complain about, mock, dismiss, downplay, badmouth, or speak
  negatively about {avoid_brands}, even by implication or through indirect
  references like "they", "them", "that company", "that product", "the
  competitor", or descriptions of the brand's category that obviously fit it.
- Do NOT compare {avoid_brands} unfavorably to anything else.
- Do NOT list cons, drawbacks, downsides, frustrations, bugs, complaints,
  pricing gripes, customer-service horror stories, or "wish they had X"
  remarks attached to {avoid_brands}.
- Do NOT relay second-hand criticism ("a friend said…", "I've heard…",
  "people complain that…") about {avoid_brands}.
- If a comment must take a position, the position toward {avoid_brands} is
  neutral or mildly positive. Never negative.
- This rule overrides any persona / structure / angle that would normally
  invite a critique.
- NO MIXED REVIEWS. Even a balanced-sounding observation is criticism if
  the second clause describes a NEGATIVE attribute of {avoid_brands}.
  Examples that LOOK factual but ARE criticism (do NOT write any of these):
    "stays cool but loses support after 6 hours"
    "great features but heats up over time"
    "solid build but takes a week to break in"
    "comfortable for the first month but drops off after that"
    "well-designed though the price creeps up"
    "good in most areas but [any drawback]"
  If you catch yourself writing "X is good but Y" about {avoid_brands},
  STRIP the "but Y" clause entirely. Don't rewrite it as "though Y" or
  "with one caveat" — those are the same criticism in different words.
- NO FACTUAL-TONED DRAWBACKS attached to {avoid_brands}. Forbidden
  clause patterns when describing the brand: "but [it/X] loses…",
  "though it does Y", "downside is…", "the only issue is…", "needs
  replacing every Y", "drops off after Y", "starts to Y after Y",
  "wears down in Y", "the catch is…", "with the trade-off of…",
  "comes with [any negative]". Even-toned phrasing of a drawback is
  still a drawback.
- Brand mentions of {avoid_brands} must be one of: positive
  ("X handles this well", "X is the one that does Y"), pure neutral
  ("X is one option here", "X is built for this use-case"), or
  factually-positive ("X uses [neutral material/feature]"). NEVER
  even-handed-with-a-drawback.
{retry_section}

{few_shot_text}

Generate exactly {num_comments} comments. Return JSON only:
{{
    "generated_comments": ["comment 1", "comment 2", ...],
    "strategies_used": ["strategy 1", "strategy 2", ...]
}}"""

        # HQ-main anchors get a touch more variety to break the body template.
        temperature = 0.95 if (retry_feedback or hq_main) else 0.9
        system_prompt = random.choice(GENERATION_SYSTEM_PROMPTS)
        max_tok = 2000 if num_comments <= 2 else 3000
        # Diagnostic — confirms focus phrases reach the runtime so we can tell
        # "deployed but ignored by the model" apart from "never reached the
        # prompt" when the user reports phrases not landing in comments.
        if focus_items:
            print(f"    [focus] brand={brand_name!r} focus={focus_items}")
        result = self.claude.call(prompt, max_tokens=max_tok, temperature=temperature, system_prompt=system_prompt)

        if not result:
            return {"generated_comments": [], "strategies_used": [], "_personas": [], "_structures": []}

        result["_personas"] = [p["id"] for p in selected_personas[:num_comments]]
        result["_structures"] = [s["id"] for s in selected_structures[:num_comments]]
        return result

    # ------------------------------------------------------------------
    # Brand-focus pairing wrapper — Layer 4 of the focus strategy.
    # ------------------------------------------------------------------

    def generate_with_focus_pairing(self, gen_kwargs, focus_assignments,
                                     mention_brand_flags, brand_name,
                                     brand_assignments=None):
        """Run generate_comments + per-slot pairing check + one retry.

        For every slot where `focus_assignments[i]` is non-None and
        `mention_brand_flags[i]` is truthy, after the initial generate
        we check whether the body contains both the brand and the phrase
        within ~120 chars (`_focus_pair_in_body`). On a miss we retry
        that single slot once via a `num_comments=1` follow-up call with
        a strict `retry_feedback`. The retry's body replaces the missed
        slot in the result. If the retry still misses, we keep the
        latest body and record `focus_hit=0`.

        Args:
          gen_kwargs: dict — kwargs to pass to self.generate_comments.
                     Must contain `num_comments`, `focus_assignments`,
                     `mention_brand_flags` consistent with the args
                     below.
          focus_assignments: list[str|None] — per-slot phrase assignment.
                     Same length as num_comments.
          mention_brand_flags: list[bool] — same length as num_comments.
          brand_name: str — single-brand fallback for proximity check.
          brand_assignments: list[dict|None] — per-slot brand dict
                     (multi-brand). When set, slot i's brand for the
                     proximity check is brand_assignments[i]['name'].

        Returns:
          (result_dict, focus_hits) where result_dict is the same shape
          generate_comments returns and focus_hits is a list[int|None]
          parallel to num_comments — 1 hit, 0 miss, None if no phrase
          was assigned.
        """
        n = int(gen_kwargs.get("num_comments", 0) or 0)
        result = self.generate_comments(**gen_kwargs)
        bodies = result.get("generated_comments") or []
        focus_hits = [None] * n

        # Helper: which brand applies to slot i for proximity check?
        def _slot_brand(i):
            if brand_assignments and i < len(brand_assignments) and brand_assignments[i]:
                return (brand_assignments[i].get("name") or brand_name)
            return brand_name

        # First pass: check each slot.
        misses = []
        for i in range(min(n, len(bodies))):
            phrase = focus_assignments[i] if i < len(focus_assignments) else None
            if not phrase:
                continue
            if not (i < len(mention_brand_flags) and mention_brand_flags[i]):
                continue
            hit, _dist = _focus_pair_in_body(_slot_brand(i), phrase, bodies[i])
            if hit:
                focus_hits[i] = 1
            else:
                focus_hits[i] = 0
                misses.append(i)

        # Single-shot retry for each miss. The retry only generates ONE
        # comment, with strict retry_feedback explaining the pairing.
        for i in misses:
            phrase = focus_assignments[i]
            slot_brand = _slot_brand(i)
            retry_kwargs = dict(gen_kwargs)
            retry_kwargs["num_comments"] = 1
            retry_kwargs["focus_assignments"] = [phrase]
            retry_kwargs["mention_brand_flags"] = [mention_brand_flags[i]]
            if brand_assignments:
                retry_kwargs["brand_assignments"] = [
                    brand_assignments[i] if i < len(brand_assignments) else None
                ]
            # Slot-overrides may have been set per-slot — narrow to slot i.
            so = gen_kwargs.get("slot_overrides")
            if isinstance(so, dict) and i in so:
                retry_kwargs["slot_overrides"] = {0: so[i]}
            elif isinstance(so, list) and i < len(so):
                retry_kwargs["slot_overrides"] = [so[i]]
            else:
                retry_kwargs["slot_overrides"] = None
            # Reply targets are also per-slot keyed.
            rt = gen_kwargs.get("reply_targets")
            if isinstance(rt, dict) and i in rt:
                retry_kwargs["reply_targets"] = {0: rt[i]}
            else:
                retry_kwargs["reply_targets"] = None
            # Stronger directive for the retry.
            retry_kwargs["retry_feedback"] = (
                "Your previous attempt did NOT pair "
                f"{slot_brand} with the phrase \"{phrase}\" in the same "
                "or adjacent sentence. The two MUST travel together — "
                "not on opposite ends of the comment, not phrase without "
                "brand, not brand without phrase. Place them within ~10 "
                "words of each other. Try again."
            )
            try:
                retry_result = self.generate_comments(**retry_kwargs)
            except Exception as e:
                print(f"    [focus] retry exception slot={i}: {e}")
                continue
            new_bodies = (retry_result or {}).get("generated_comments") or []
            if not new_bodies:
                continue
            # Replace the missed body with the retry's body.
            bodies[i] = new_bodies[0]
            hit, _dist = _focus_pair_in_body(slot_brand, phrase, bodies[i])
            focus_hits[i] = 1 if hit else 0

        # Mutate result in place so the caller's view stays consistent.
        result["generated_comments"] = bodies

        # Telemetry — single line per batch summarising hits/misses.
        assigned_n = sum(1 for h in focus_hits if h is not None)
        if assigned_n:
            hits_n = sum(1 for h in focus_hits if h == 1)
            miss_n = assigned_n - hits_n
            print(f"    [focus] brand={brand_name!r} assigned={assigned_n} "
                  f"hit={hits_n} miss={miss_n}")

        return result, focus_hits

    # ------------------------------------------------------------------
    # Validation (preserved from original)
    # ------------------------------------------------------------------

    def validate_comments(self, post_title, post_body, subreddit, comments,
                          brand_name, generated_comments, tone_analysis=None,
                          ai_crawl=False, post_intent=None, is_hq_reply=False):
        """LLM-driven quality gate for generated comments.

        Each comment is scored on four binary judgments by an LLM rubric:
          (a) brand_attitude     — positive | neutral | negative (FAIL on negative)
          (b) answers_post_query — yes | no                       (FAIL on no)
          (c) shape_match        — yes | no                       (FAIL on no)
          (d) authenticity       — yes | no                       (FAIL on no)

        PASS = all four. Caller retries with the rubric's natural-language
        feedback as `retry_feedback`. After 2 retries, drop the comment.

        A narrow set of cheap programmatic pre-filters (dashes, brand
        sentence-ratio, narrow BANNED_PHRASES) still runs as a fast-fail.
        The long lists of "criticism words" and "AI tells" have been
        retired — that's the LLM rubric's job, by judgment, not regex.
        """
        if not generated_comments:
            return {"evaluations": [], "any_failed": True}

        # Use the SHARED intent classifier — same signal as the generator
        # so rubric strictness aligns with what we asked the model to write.
        intent_info = classify_post_intent(post_title, post_body, post_intent)
        is_recommendation = intent_info["is_recommendation"]
        post_intent_label = post_intent or intent_info["intent_label"]

        gen_text = "\n".join([
            f'Comment {i+1}: """{comment}"""' for i, comment in enumerate(generated_comments)
        ])

        ai_crawl_strictness = ""
        if ai_crawl and not is_hq_reply:
            ai_crawl_strictness = (
                "\nAI-CRAWL MODE is ON. Apply stricter shape_match: the comment must "
                "demonstrably reinforce the post title's underlying long-tail query "
                "(re-read the post title — that IS the query an AI search engine would "
                "match). For recommendation-seeking posts, the comment must read as a "
                "direct answer (recommendation, criteria, comparison) — NOT a "
                "personal-journey ramble that opens with answer-framing then pivots "
                "to complaining about the category."
            )

        rec_shape_hint = ""
        if is_recommendation:
            rec_shape_hint = (
                "\nThis post is RECOMMENDATION-SEEKING. shape_match passes only if the "
                "comment is answer-shaped: it gives a recommendation, criteria, or "
                "comparison. shape_match FAILS for personal-journey rambles, "
                "anecdotes that don't land on an answer, and 'answer-framing then "
                "pivot to complaining' hybrids."
            )

        prompt = f"""You are a strict Reddit comment quality reviewer. For each generated
comment below, decide whether it is GOOD ENOUGH to post under the given
post, with the given brand attached. Be skeptical — if anything in the
comment would embarrass a brand-marketing reviewer or tip off a Reddit
user as AI-generated, fail it.

POST TITLE: "{post_title}"
POST BODY: {(post_body or '')[:600]}
SUBREDDIT: r/{subreddit}
BRAND: {brand_name}
POST INTENT: {post_intent_label}
{rec_shape_hint}{ai_crawl_strictness}

GENERATED COMMENTS:
{gen_text}

For each comment, score these FOUR binary judgments. For each one, write
a one-sentence reason BEFORE deciding pass/fail. State what you observe.

(a) brand_attitude — does the comment criticize, complain about, mock,
    downplay, or otherwise speak negatively about "{brand_name}"?
    Negative includes:
      - Direct criticism ("X is overpriced", "X has bad customer service")
      - Indirect criticism via the brand's category ("most of these tools
        require a ton of post work" when the brand is one of those tools)
      - Problem-then-brand arcs ("I gave up and tried X, but even then I
        had to tweak everything manually")
      - Second-hand criticism ("a friend says X doesn't work that well")
      - Unfavorable comparison ("X is fine, but Y is way better")
    Score: positive | neutral | negative. FAIL if negative.

(b) answers_post_query — re-read the POST TITLE and BODY. Is this comment
    a useful answer to what the post is asking? For
    recommendation-seeking posts, useful = the comment gives a
    recommendation, criteria for choosing, or comparison **that an AI
    search engine could extract as an answer to the post's query**.
    A stranger reading just this comment (no other context) must be
    able to identify what is being recommended. A personal anecdote
    that buries or hedges the answer is NOT useful — even if it
    technically mentions a tool name.
    Specifically FAIL the comment if:
      - It opens with "Funny you mention this", "I was just helping my
        cousin/friend/wife/colleague", "Just last week I…", or any
        anecdote framing that buries the answer.
      - It hedges the brand mention: "(think one was called X or
        something)", "I tried X or whatever it was", "maybe X?", "if
        I remember correctly it was X". Confident mentions only.
      - It ends on meta-commentary instead of the answer ("the
        technology is basically already there", "creators waste so
        much time on this", "anyway, ended up spending way too much
        time experimenting").
    For experience-sharing posts, a relevant observation or related
    anecdote counts.
    Score: yes | no. FAIL if no.

(c) shape_match — would a real Reddit user, replying under this post's
    intent, write a comment with this shape? Recommendation post →
    answer shape passes; personal-journey ramble (cousin / food truck /
    "down the rabbit hole" framing) fails even if it eventually mentions
    the brand. Experience post → anecdote passes, stiff listicle fails.
    Generic explainer that ignores the post's specifics fails either way.
    Score: yes | no. FAIL if no.

(d) authenticity — does this read as genuinely human, or AI-generated?
    Tells include:
      - Throat-clearing openers anywhere (not just first word):
        "honestly", "the reality is", "the truth is", "at the end of
        the day", "still figuring out", "from my experience", "I'm still
        learning", "I had the same problem"
      - "Answer-framing then pivot to complaining" hybrids
      - Stiff listicle structure where a Redditor wouldn't use one
      - Over-polished or marketing-flavored phrasing
      - Hollow generic statements that could fit any post
    Score: yes | no. FAIL if no.

PASS = brand_attitude is positive OR neutral, AND answers_post_query=yes,
AND shape_match=yes, AND authenticity=yes.

Return JSON only:
{{
    "evaluations": [
        {{
            "comment_index": 0,
            "brand_attitude": "positive" | "neutral" | "negative",
            "brand_attitude_reason": "one sentence",
            "answers_post_query": "yes" | "no",
            "answers_post_query_reason": "one sentence",
            "shape_match": "yes" | "no",
            "shape_match_reason": "one sentence",
            "authenticity": "yes" | "no",
            "authenticity_reason": "one sentence",
            "pass": true | false,
            "feedback": "concatenation of all FAIL reasons, empty if pass"
        }}
    ],
    "any_failed": true | false
}}"""

        # The rubric output is 4 short reasons + judgments per comment.
        # 800 tokens is plenty for 5 comments; lowering this from 1500 to 800
        # noticeably cuts validator round-trip time.
        result = self.claude.call(prompt, max_tokens=800, temperature=0.2)
        if not result:
            # If the validator itself fails, do not block the pipeline;
            # let the comment through with a noted-but-not-failing eval.
            return {"evaluations": [
                {"comment_index": i, "pass": True, "feedback": "(validator unavailable)"}
                for i in range(len(generated_comments))
            ], "any_failed": False}

        evals = result.get("evaluations", [])

        # Reconcile pass/fail from the four rubric items (the LLM may set
        # pass=true while flagging a fail elsewhere; recompute).
        for ev in evals:
            fails = []
            if ev.get("brand_attitude") == "negative":
                fails.append(f"brand_attitude=negative: {ev.get('brand_attitude_reason', '')}".strip())
            if ev.get("answers_post_query") == "no":
                fails.append(f"answers_post_query=no: {ev.get('answers_post_query_reason', '')}".strip())
            if ev.get("shape_match") == "no":
                fails.append(f"shape_match=no: {ev.get('shape_match_reason', '')}".strip())
            if ev.get("authenticity") == "no":
                fails.append(f"authenticity=no: {ev.get('authenticity_reason', '')}".strip())
            if fails:
                ev["pass"] = False
                ev["feedback"] = " | ".join(fails)
            else:
                ev["pass"] = True
                ev.setdefault("feedback", "")

        brand_lower = brand_name.lower()

        # Narrow programmatic pre-filter — cheap, unambiguous failures.
        # The long lists of "criticism words", "AI tells", and problem
        # precursors have been retired; the LLM rubric above handles
        # those judgments by reading the comment, not by regex.
        for ev_idx, ev in enumerate(evals):
            if ev_idx >= len(generated_comments):
                continue
            comment_text = generated_comments[ev_idx]
            comment_lower = comment_text.lower()

            # Dashes — never allowed (we control via prompt)
            if re.search(r'[\u2013\u2014]| - |--', comment_text):
                ev["pass"] = False
                ev["feedback"] = ("Contains dashes (-, em-dash, --). " + (ev.get("feedback") or "")).strip()

            # Marketing-speak BANNED_PHRASES (narrow, unambiguous)
            found = [p for p in BANNED_PHRASES if p in comment_lower]
            if found:
                ev["pass"] = False
                ev["feedback"] = (f"Banned marketing phrases: {found}. " + (ev.get("feedback") or "")).strip()

            # Brand-sentence ratio — over-mention is structural promotion
            if brand_lower in comment_lower:
                sentences = comment_lower.replace('!', '.').replace('?', '.').split('.')
                sentences = [s.strip() for s in sentences if s.strip()]
                if sentences:
                    brand_sentences = sum(1 for s in sentences if brand_lower in s)
                    if brand_sentences / len(sentences) > 0.3:
                        ev["pass"] = False
                        ev["feedback"] = (
                            f"Brand in {brand_sentences}/{len(sentences)} sentences (over-mentioning). "
                            + (ev.get("feedback") or "")
                        ).strip()

        result["any_failed"] = any(not ev.get("pass") for ev in evals)
        return result

    # ------------------------------------------------------------------
    # Validate-and-retry wrapper (the one quality gate every comment
    # generation flow goes through). Drop-in replacement for
    # self.generate_comments(...) — same kwargs, same return shape — but
    # the returned `generated_comments` only contains comments that
    # passed validation. Up to `max_retries` retries per failed comment,
    # each retry seeded with the validator's natural-language feedback.
    # ------------------------------------------------------------------
    def _generate_with_validation(self, max_retries=1, **kwargs):
        post_title   = kwargs.get("post_title", "")
        post_body    = kwargs.get("post_body", "")
        subreddit    = kwargs.get("subreddit", "")
        comments     = kwargs.get("comments", []) or []
        brand_name   = kwargs.get("brand_name", "")
        tone_analysis = kwargs.get("tone_analysis")
        ai_crawl     = kwargs.get("ai_crawl", False)
        post_intent  = kwargs.get("post_intent", None)
        is_hq_reply  = kwargs.get("is_hq_reply", False)

        # Initial generation (full batch)
        result = self.generate_comments(**kwargs)
        bodies     = list(result.get("generated_comments") or [])
        personas   = list(result.get("_personas") or [])
        structures = list(result.get("_structures") or [])
        strategies = list(result.get("strategies_used") or [])
        if not bodies:
            return result

        # Validate the whole batch in one call
        val = self.validate_comments(
            post_title=post_title, post_body=post_body, subreddit=subreddit,
            comments=comments, brand_name=brand_name,
            generated_comments=bodies, tone_analysis=tone_analysis,
            ai_crawl=ai_crawl, post_intent=post_intent, is_hq_reply=is_hq_reply,
        )
        evals = val.get("evaluations") or []

        # For each comment that failed: retry with feedback. Up to max_retries.
        final_bodies, final_personas, final_structures = [], [], []
        for idx, body in enumerate(bodies):
            ev = evals[idx] if idx < len(evals) else {"pass": True, "feedback": ""}
            persona   = personas[idx]   if idx < len(personas)   else None
            structure = structures[idx] if idx < len(structures) else None

            if ev.get("pass"):
                final_bodies.append(body)
                final_personas.append(persona)
                final_structures.append(structure)
                continue

            # Retry just this one slot. Project the per-comment kwargs
            # down to a 1-comment generation so the prompt is focused.
            retry_kwargs = dict(kwargs)
            retry_kwargs["num_comments"] = 1
            mbf = retry_kwargs.get("mention_brand_flags") or []
            if mbf:
                retry_kwargs["mention_brand_flags"] = [mbf[idx] if idx < len(mbf) else False]
            ba = retry_kwargs.get("brand_assignments") or []
            if ba:
                retry_kwargs["brand_assignments"] = [ba[idx] if idx < len(ba) else None]
            rt = retry_kwargs.get("reply_targets")
            if isinstance(rt, dict):
                # original key was the comment index in the batch; on retry
                # we collapse to a single-comment batch keyed at index 0.
                retry_kwargs["reply_targets"] = {0: rt[idx]} if idx in rt else None

            passed = False
            last_feedback = ev.get("feedback") or "Comment failed validation."
            for attempt in range(max_retries):
                print(f"    [validate] comment {idx+1} failed: {last_feedback[:200]}")
                print(f"    [validate] retry {attempt+1}/{max_retries}")

                # SMART RETRY: don't just feed feedback through — change
                # the SHAPE. Same persona+structure on retry tends to
                # produce the same failure. Force a different starting
                # point based on which judgment failed.
                intent_info = classify_post_intent(post_title, post_body, post_intent)
                last_ev = ev if attempt == 0 else None  # most recent fail eval
                forced_persona = forced_structure = None
                if (ev.get("shape_match") == "no" or "shape_match" in (last_feedback or "")) \
                        and intent_info["is_recommendation"]:
                    # Wrong shape on a recommendation post → force direct-answer
                    rec_ps = [p for p in PERSONAS if p["id"] in {"helper", "veteran_terse", "professional"}]
                    rec_ss = [s for s in STRUCTURE_TEMPLATES if s["id"] in {"direct_answer", "short_punchy"}]
                    if rec_ps: forced_persona = random.choice(rec_ps)
                    if rec_ss: forced_structure = random.choice(rec_ss)
                elif ev.get("authenticity") == "no" or "authenticity" in (last_feedback or ""):
                    # AI-tells → swap to a different persona category. If the
                    # current persona was DIRECT, switch to ANECDOTE; vice
                    # versa. Real Reddit voices have variety.
                    cur_cat = None
                    for cat, ids in PERSONA_CATEGORIES.items():
                        if persona in ids:
                            cur_cat = cat
                            break
                    swap_cat = "ANECDOTE" if cur_cat == "DIRECT" else "DIRECT"
                    swap_ids = PERSONA_CATEGORIES[swap_cat]
                    swap_ps = [p for p in PERSONAS if p["id"] in swap_ids]
                    if swap_ps: forced_persona = random.choice(swap_ps)
                # Inject the forced persona/structure into the retry kwargs
                # by short-circuiting _select_comment_config — we override
                # via brand_assignments/relevance? No, simpler path: pass
                # via the prompt's per-comment LENGTH section by stuffing
                # an explicit "force_persona" hint into retry_feedback.
                shape_hint = ""
                if forced_persona:
                    shape_hint += (
                        f"\nFORCE PERSONA for retry: {forced_persona['voice']}"
                    )
                if forced_structure:
                    shape_hint += (
                        f"\nFORCE STRUCTURE for retry: {forced_structure['instruction']}"
                    )
                retry_kwargs["retry_feedback"] = last_feedback + shape_hint

                r2 = self.generate_comments(**retry_kwargs)
                new_bodies = r2.get("generated_comments") or []
                if not new_bodies:
                    continue
                new_body = new_bodies[0]
                v2 = self.validate_comments(
                    post_title=post_title, post_body=post_body, subreddit=subreddit,
                    comments=comments, brand_name=brand_name,
                    generated_comments=[new_body], tone_analysis=tone_analysis,
                    ai_crawl=ai_crawl, post_intent=post_intent, is_hq_reply=is_hq_reply,
                )
                ev2 = (v2.get("evaluations") or [{}])[0]
                if ev2.get("pass"):
                    final_bodies.append(new_body)
                    final_personas.append((r2.get("_personas") or [persona])[0])
                    final_structures.append((r2.get("_structures") or [structure])[0])
                    print(f"    [validate] comment {idx+1} passed on retry {attempt+1}")
                    passed = True
                    break
                last_feedback = ev2.get("feedback") or last_feedback
            if not passed:
                print(f"    [validate] comment {idx+1} dropped after {max_retries} retries")

        return {
            "generated_comments": final_bodies,
            "_personas": final_personas,
            "_structures": final_structures,
            "strategies_used": strategies,
        }

    # ------------------------------------------------------------------
    # NEW: Comment tree generation for fresh posts
    # ------------------------------------------------------------------

    def generate_comment_tree(self, post, brand_or_brands, num_comments,
                               brand_mention_ratio=None, post_day_offset=0,
                               brands_config=None, op_reply_count=0,
                               ai_crawl=False):
        """Generate a full comment tree for a fresh post (no existing Reddit comments).

        Args:
            post: post dict from DB
            brand_or_brands: single brand dict (backward compat) OR ignored if brands_config given
            num_comments: total number of comments (top-level + replies + OP replies)
            brand_mention_ratio: fraction of comments that mention brand (single-brand mode)
            post_day_offset: the day the post is scheduled for
            brands_config: list of {"brand": brand_dict, "mention_count": int} for multi-brand
            op_reply_count: number of OP replies to include in the tree
            ai_crawl: when True, each comment's prompt gains an AI-CRAWL NOTE
                      block (substance, brand-domain vocabulary, long-tail
                      query phrasing) so the thread is more retrievable by
                      ChatGPT / Perplexity / Claude. Used by Live Subreddits.

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
        top_mention_flags = mention_flags[:num_top]
        # Per-slot focus phrase assignment. The slot-i brand for the
        # relevance gate is the per-slot assigned brand if multi-brand,
        # else the primary brand. We compute one assignment list across
        # the batch — _assign_focus_phrases returns None for non-brand
        # slots automatically, so reply-only and no-brand slots are
        # untouched.
        primary_focus_items = self._extract_brand_focus(primary_brand)
        top_focus_assignments = _assign_focus_phrases(
            primary_focus_items, post, top_mention_flags
        )
        top_level_result = self._generate_with_validation(
            post_title=post["title"],
            post_body=post["body"],
            subreddit=subreddit["name"],
            comments=[],  # no existing comments
            brand_name=primary_brand["name"],
            brand_context=primary_brand["context"],
            num_comments=num_top,
            tone_analysis=mock_tone,
            comment_stats=mock_stats,
            mention_brand_flags=top_mention_flags,
            relevance={"best_angle": "general discussion", "natural_fit": 2},
            brand_assignments=top_assignments,
            all_brand_names=all_brand_names,
            ai_crawl=ai_crawl,
            post_intent=post.get("intent"),
            brand_focus=primary_focus_items,
            focus_assignments=top_focus_assignments,
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

            # Focus pairing check: did the assigned phrase land within
            # ~120 chars of the brand mention? Persist the result so the
            # UI can show a green/amber chip per row and the coverage
            # endpoint can aggregate hit/miss counts per phrase.
            slot_focus = top_focus_assignments[i] if i < len(top_focus_assignments) else None
            slot_focus_hit = None
            if slot_focus and mentions:
                slot_brand_name = (assigned or primary_brand)["name"]
                hit, _dist = _focus_pair_in_body(slot_brand_name, slot_focus, body)
                slot_focus_hit = 1 if hit else 0
            elif slot_focus and not mentions:
                # Phrase was assigned but the model didn't end up
                # mentioning the brand — pairing impossible.
                slot_focus_hit = 0

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
                focus_phrase=slot_focus,
                focus_hit=slot_focus_hit,
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

                reply_result = self._generate_with_validation(
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
                    ai_crawl=ai_crawl,
                    post_intent=post.get("intent"),
                    brand_focus=self._extract_brand_focus(reply_brand),
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

    def generate_hq_comment(self, post, brand, brand_mention_ratio=None, post_day_offset=0,
                             ai_crawl=False, num_replies=5, concept_checklist=None):
        """Generate one high-quality top-level comment with brand mention plus
        `num_replies` relevant replies forming a realistic nested conversation.

        Total comments saved = 1 + num_replies. Caller may pass any num_replies >= 1;
        the shape is generated dynamically (most replies hang off main, ~1/3 nest
        under earlier replies for thread realism).

        ai_crawl=True (used by Live Subreddits) injects an AI-CRAWL NOTE rule
        into the per-comment prompt so the thread is more retrievable by AI
        search engines (substance + brand-domain vocab + long-tail query phrasing).

        The main comment (index 0) always mentions the brand. Replies do not.
        Thread shapes are randomized — some replies target the main comment,
        others reply to earlier replies.
        """
        # brand_mention_ratio is ignored — main comment always mentions brand

        # --- Thread shape ---------------------------------------------------
        # 1 main (idx 0) + num_replies replies. Some replies hang off main,
        # ~30% nest under an earlier reply for realism.
        nr = max(1, int(num_replies))
        shape = [(0, None)]
        nest_count = nr // 3   # how many replies nest under earlier replies
        direct_count = nr - nest_count
        for i in range(1, direct_count + 1):
            shape.append((i, 0))
        for i in range(direct_count + 1, nr + 1):
            # nest under a random earlier reply (not main, not future)
            candidates = list(range(1, i))
            parent_idx = random.choice(candidates) if candidates else 0
            shape.append((i, parent_idx))

        # --- Brand mentions --------------------------------------------------
        # Main comment (index 0) always mentions brand, replies never do
        mention_flags = [True] + [False] * nr

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

        # Pre-select distinct personas/structures (1 main + num_replies)
        all_personas, all_structures, all_angles = self._select_comment_config(
            mock_tone, hq_stats,
            {"best_angle": "detailed thoughtful response", "natural_fit": 3},
            len(shape),
        )

        # Pre-allocate a balanced mix of REPLY shapes for this cluster.
        # Forces variety across slots (at least one follow-up question,
        # at most one medium_add, oneliner cap) and pins per-slot persona
        # / structure / angle / word range. Indexed 0..nr-1; used at
        # idx-1 inside _gen_one (idx 0 is main, replies start at idx 1).
        reply_shapes = _allocate_reply_shapes(nr) if nr > 0 else []

        saved = []          # list of dicts with id, body, etc.
        saved_ids = {}       # index -> DB comment id
        saved_bodies = {}    # index -> body text
        saved_personas = {}  # index -> persona id

        # ------------------------------------------------------------------
        # Parallel-by-level generation. HQ replies are I/O-bound on the
        # Anthropic API; firing siblings concurrently collapses what was a
        # 5-call sequential chain into ~3 wall-time rounds (main → level-1
        # parallel → level-2 parallel). We never drop a comment due to
        # validation here — the user explicitly wants every HQ slot saved
        # — so this path uses raw generate_comments and one retry on
        # genuine API failure (empty response).
        # ------------------------------------------------------------------
        print(f"    Generating HQ comment thread (1 main + {nr} replies, parallel-by-level)...")
        _hq_t0 = time.time()

        # Worker: one comment generation. Returns a dict (or None on API failure).
        # Pure read of `self`; no shared state mutated. Safe to run concurrently.
        def _gen_one(idx, parent_idx, parent_body, sibling_bodies, persona_id, structure_id):
            is_main_local = parent_idx is None
            # CONTEXT: for HQ MAIN, pass no thread context — main is the
            # first comment. For HQ REPLIES, the parent body flows in via
            # `reply_targets` (so the model knows what it's replying to) AND
            # already-generated SIBLING replies (other replies under the same
            # parent, e.g. the anchor reply produced in the first phase of
            # this level) flow in via `comments` with the in_thread_siblings
            # flag — that flag swaps the standard `EXISTING COMMENTS:` block
            # for an explicit "OTHER REPLIES IN THIS HQ THREAD" instruction
            # telling the model not to echo their opener / their angle / the
            # parent key-phrase they already used. Without this the parallel
            # siblings reliably both opened with the same echo of the parent.
            thread_comments = []
            reply_targets = None
            if parent_body is not None:
                reply_targets = {0: {
                    "body": parent_body, "score": 5,
                    "author": "community_member", "id": "", "permalink": "",
                }}
                if sibling_bodies:
                    thread_comments = [
                        {"body": sb, "score": 5, "author": "community_member"}
                        for sb in sibling_bodies if sb
                    ]
            # REPLY SHAPE OVERRIDE: replies use the pre-allocated shape
            # (one-liner agree / short pushback / follow-up Q / short add /
            # dry one-liner / rare medium add) instead of the random
            # persona+composite-angle path. The shape pins persona,
            # structure, sentence/word target, and a single-move angle —
            # which fixes the "long meaningless paragraph" pattern at the
            # source. Main is unchanged: it still gets the brand-mention
            # decisive framing and intent-driven length scaling.
            shape = None
            best_angle = ""
            slot_overrides = None
            if is_main_local:
                # Two best-angle modes for the HQ root, gated by the
                # AI-crawl toggle:
                #   ai_crawl=True  → today's hard-recommendation
                #     framing (lead with brand, tight 50-90 words,
                #     no anecdote). Pairs with the HQ-MAIN OVERRIDE
                #     and AI-CRAWL prompt blocks for AI-retriever-
                #     shaped output.
                #   ai_crawl=False → conversational Live-Search-style
                #     framing. Root still mentions the brand once
                #     (mention_flags[0]=True is independent of this),
                #     but in passing — no extractable-answer pattern.
                if ai_crawl:
                    # Vary the OPENING approach per post so HQ anchors don't all
                    # read identically — same strategy (confident, extractable,
                    # brand named early, tight 50-90 words, no anecdote), different
                    # way in. Mirrors the OP-affirm `_open_angle` pool.
                    _hq_angle = random.choice([
                        f"Lead with {brand['name']} as the direct answer, then say what it does for the OP's use-case.",
                        f"Open by naming the specific problem the OP has, then immediately point to {brand['name']} as what solves it.",
                        f"State the criteria that actually matter here, then name {brand['name']} as the pick that meets them.",
                        f"Open with {brand['name']} and the one capability that makes it fit this exact situation.",
                        f"Recommend {brand['name']} up front, then briefly contrast with the generic options people default to.",
                        f"Answer the OP's question in the first line by naming {brand['name']}, then back it with what it does.",
                        f"Open with the outcome the OP is after and name {brand['name']} as how to get there.",
                        f"Name {brand['name']} as the go-to for this niche first, then give the concrete reason it fits.",
                    ])
                    best_angle = (
                        f"Recommend {brand['name']} confidently as a direct answer to "
                        f"the OP's question. {_hq_angle} Tight 50-90 words, no anecdote."
                    )
                else:
                    best_angle = (
                        f"Share a relevant observation that mentions "
                        f"{brand['name']} naturally in passing. "
                        "Conversational, no hard recommendation framing — "
                        "talk like a real Reddit commenter chiming in."
                    )
            else:
                # idx 0 is main; replies are 1..nr — map to 0-indexed shape array.
                shape = reply_shapes[idx - 1] if 0 <= idx - 1 < len(reply_shapes) else _pick_reply_shape()
                best_angle = shape["angle"]
                slot_overrides = {0: shape}

            # Focus pairing — only the HQ MAIN slot is brand-mention,
            # so it's the only slot eligible for a focus phrase. Replies
            # never carry one (no brand to associate with).
            slot_focus_phrase = None
            focus_assignments_arg = None
            if is_main_local:
                focus_items = self._extract_brand_focus(brand)
                main_focus_list = _assign_focus_phrases(
                    focus_items, post, [True]
                )
                slot_focus_phrase = main_focus_list[0] if main_focus_list else None
                if slot_focus_phrase:
                    focus_assignments_arg = [slot_focus_phrase]

            t0 = time.time()
            try:
                result = self.generate_comments(
                    post_title=post["title"],
                    post_body=post["body"],
                    subreddit=subreddit["name"],
                    comments=thread_comments,
                    brand_name=brand["name"],
                    brand_context=brand["context"],
                    num_comments=1,
                    tone_analysis=mock_tone,
                    comment_stats=hq_stats if is_main_local else reply_stats,
                    mention_brand_flags=[mention_flags[idx]],
                    reply_targets=reply_targets,
                    relevance={
                        "best_angle": best_angle,
                        "natural_fit": 3,
                    },
                    ai_crawl=ai_crawl,
                    post_intent=post.get("intent"),
                    # HQ-MAIN-OVERRIDE is now gated by the AI-crawl
                    # toggle: when OFF the root falls through the
                    # regular generate_comments path (no hard
                    # "lead with the recommendation" framing),
                    # producing a Live-Search-style comment that
                    # still mentions the brand (mention_flags drives
                    # that, not hq_main). When ON, behavior is
                    # identical to before — the HQ-MAIN block layers
                    # on top of the AI-CRAWL block for the root.
                    hq_main=is_main_local and ai_crawl,
                    # Replies use the slim prompt to skip ~1500 tokens of
                    # few-shot anti-pattern examples — meaningful speedup
                    # since each reply is its own API call.
                    slim_prompt=not is_main_local,
                    brand_focus=self._extract_brand_focus(brand),
                    in_thread_siblings=bool(thread_comments),
                    is_hq_reply=not is_main_local,
                    slot_overrides=slot_overrides,
                    focus_assignments=focus_assignments_arg,
                    # Only the anchor (main) slot covers the cluster checklist.
                    concept_checklist=concept_checklist if is_main_local else None,
                )
            except Exception as e:
                print(f"    [HQ] gen exception idx={idx}: {e}")
                return None
            elapsed = time.time() - t0
            kind = "main" if is_main_local else f"reply{idx}"
            print(f"    [HQ] {kind} generated in {elapsed:.1f}s")
            bodies = result.get("generated_comments") or []
            if not bodies:
                return None
            # Run pairing check on main if a phrase was assigned. We
            # don't do an inline retry here — the stricter per-slot
            # directive in the prompt does the heavy lifting; a miss
            # is recorded for telemetry / coverage stats.
            focus_hit = None
            if slot_focus_phrase:
                hit, _dist = _focus_pair_in_body(brand["name"], slot_focus_phrase, bodies[0])
                focus_hit = 1 if hit else 0
            return {
                "idx": idx,
                "body": bodies[0],
                "persona_id": (result.get("_personas") or [persona_id])[0],
                "structure_id": (result.get("_structures") or [structure_id])[0],
                "focus_phrase": slot_focus_phrase,
                "focus_hit": focus_hit,
            }

        # Helper: persist one generated comment to DB and update the
        # in-memory bookkeeping. Called single-threaded after each level.
        def _save_one(idx, parent_idx, gen_result):
            body = gen_result["body"]
            mentions = mention_flags[idx] and brand["name"].lower() in body.lower()
            if parent_idx is None:
                comment_day = post_day_offset
            elif parent_idx == 0:
                comment_day = post_day_offset + 1
            else:
                comment_day = post_day_offset + 2
            cid = self.db.save_comment(
                post_id=post["id"],
                brand_id=brand["id"],
                body=body,
                persona_id=gen_result["persona_id"],
                structure_id=gen_result["structure_id"],
                is_reply=0 if parent_idx is None else 1,
                parent_comment_id=saved_ids.get(parent_idx) if parent_idx is not None else None,
                mentions_brand=1 if mentions else 0,
                status="complete",
                suggested_post_day=comment_day,
                suggested_order=idx,
                prompt_version=PROMPT_VERSION,
                comment_type="hq",
                focus_phrase=gen_result.get("focus_phrase"),
                focus_hit=gen_result.get("focus_hit"),
            )
            self._detect_and_store_keywords(cid, body, brand, mentions)
            saved_ids[idx] = cid
            saved_bodies[idx] = body
            saved_personas[idx] = gen_result["persona_id"]
            saved.append({
                "id": cid, "body": body,
                "is_reply": parent_idx is not None,
                "mentions_brand": mentions,
                "day": comment_day,
                "parent_id": saved_ids.get(parent_idx) if parent_idx is not None else None,
            })
            fp = self._extract_pattern_fingerprint(
                body, brand["name"], gen_result["persona_id"], gen_result["structure_id"]
            )
            self._pattern_history.append(fp)

        # ---- Step 1: generate MAIN sequentially (replies depend on it) ----
        main_pid = all_personas[0] if all_personas else random.choice(PERSONAS)["id"]
        main_sid = all_structures[0] if all_structures else random.choice(STRUCTURE_TEMPLATES)["id"]
        r_main = _gen_one(0, None, None, [], main_pid, main_sid)
        # One retry on genuine API failure (empty response). User explicitly
        # asked for "no case where comments are not generated" — so we keep
        # whatever main produces; we do not run the LLM validator on HQ.
        if r_main is None:
            print("    [HQ] main returned no body — retrying once")
            r_main = _gen_one(0, None, None, [], main_pid, main_sid)
        if r_main is None:
            print("    HQ: main API failed twice — aborting thread")
            return saved
        _save_one(0, None, r_main)

        # ---- Step 2: build dependency tree, generate level-by-level ----
        children = {}
        for idx, parent_idx in shape[1:]:  # skip main
            children.setdefault(parent_idx, []).append(idx)

        current_level = [0]
        level_num = 1
        while True:
            next_level = []
            for p in current_level:
                next_level.extend(children.get(p, []))
            if not next_level:
                break

            # ANCHOR-FIRST PATTERN: at each level, replies sharing the SAME
            # parent are the ones most likely to echo each other (same parent
            # key-phrase, same angle). For each such parent-group with >1
            # reply we generate the FIRST reply sequentially as the anchor,
            # then submit the rest in parallel with the anchor's body
            # included in their `EXISTING COMMENTS` context (rendered as
            # "OTHER REPLIES IN THIS HQ THREAD" via in_thread_siblings).
            # Replies that have no siblings at this level just go straight
            # into the parallel pool. Wall time stays at one round per level
            # because the anchor phase only adds one extra sequential call
            # for groups that would have been fully parallel before — and
            # for the typical nr=4 shape this collapses cleanly.
            by_parent = {}
            for idx in next_level:
                p_idx = next(p for i, p in shape if i == idx)
                if p_idx not in saved_bodies:
                    p_idx = 0  # re-parent to main if intended parent failed
                by_parent.setdefault(p_idx, []).append(idx)

            anchor_idxs = set()
            anchors = {}  # parent_idx -> anchor body
            for p_idx, idxs in by_parent.items():
                if len(idxs) > 1:
                    a_idx = idxs[0]
                    anchor_idxs.add(a_idx)
                    parent_body = saved_bodies[p_idx]
                    pid = all_personas[a_idx] if a_idx < len(all_personas) else random.choice(PERSONAS)["id"]
                    sid = all_structures[a_idx] if a_idx < len(all_structures) else random.choice(STRUCTURE_TEMPLATES)["id"]
                    print(f"    [HQ] level {level_num}: anchor reply {a_idx} (parent {p_idx})")
                    r_anchor = _gen_one(a_idx, p_idx, parent_body, [], pid, sid)
                    if r_anchor is None:
                        print(f"    [HQ] anchor reply {a_idx} returned no body — retrying once")
                        r_anchor = _gen_one(a_idx, p_idx, parent_body, [], pid, sid)
                    if r_anchor is None:
                        print(f"    [HQ] anchor reply {a_idx} unrecoverable — falling back, group will be parallel")
                        anchor_idxs.discard(a_idx)
                        continue
                    _save_one(a_idx, p_idx, r_anchor)
                    anchors[p_idx] = r_anchor["body"]

            # Remaining replies (non-anchors and singletons) go in parallel.
            rest_meta = []
            for idx in next_level:
                if idx in anchor_idxs and idx in saved_bodies:
                    continue  # anchor already saved
                p_idx = next(p for i, p in shape if i == idx)
                if p_idx not in saved_bodies:
                    p_idx = 0
                parent_body = saved_bodies[p_idx]
                pid = all_personas[idx] if idx < len(all_personas) else random.choice(PERSONAS)["id"]
                sid = all_structures[idx] if idx < len(all_structures) else random.choice(STRUCTURE_TEMPLATES)["id"]
                anchor_body = anchors.get(p_idx)
                sibling_bodies = [anchor_body] if anchor_body else []
                rest_meta.append((idx, p_idx, parent_body, sibling_bodies, pid, sid))

            if rest_meta:
                print(f"    [HQ] level {level_num}: generating {len(rest_meta)} non-anchor replies in parallel")
                level_results = []
                with ThreadPoolExecutor(max_workers=min(8, len(rest_meta))) as ex:
                    fut_to_meta = {}
                    for meta in rest_meta:
                        idx, p_idx, parent_body, sibling_bodies, pid, sid = meta
                        fut = ex.submit(_gen_one, idx, p_idx, parent_body,
                                        sibling_bodies, pid, sid)
                        fut_to_meta[fut] = meta
                    for fut in as_completed(fut_to_meta):
                        meta = fut_to_meta[fut]
                        try:
                            r = fut.result()
                        except Exception as e:
                            print(f"    [HQ] worker exception idx={meta[0]}: {e}")
                            r = None
                        level_results.append((meta, r))

                # Retry once (sequentially, to bound load) any that returned None
                for meta, r in level_results:
                    if r is not None:
                        continue
                    idx, p_idx, parent_body, sibling_bodies, pid, sid = meta
                    print(f"    [HQ] reply {idx} returned no body — retrying once")
                    r2 = _gen_one(idx, p_idx, parent_body, sibling_bodies, pid, sid)
                    for j, (m, rr) in enumerate(level_results):
                        if m[0] == idx:
                            level_results[j] = (m, r2)
                            break

                # Save (single-threaded; SQLite + bookkeeping)
                for meta, r in level_results:
                    idx, p_idx, parent_body, sibling_bodies, pid, sid = meta
                    if r is None:
                        print(f"    [HQ] reply {idx} unrecoverable — skipping")
                        continue
                    _save_one(idx, p_idx, r)

            current_level = next_level
            level_num += 1

        print(f"    HQ thread complete — {len(saved)} comments generated in {time.time()-_hq_t0:.1f}s")
        return saved

    # ------------------------------------------------------------------
    # Append more replies to an existing HQ cluster
    # ------------------------------------------------------------------
    def add_replies_to_hq_cluster(self, root_comment_id, num_replies=3,
                                   ai_crawl=False):
        """Generate `num_replies` more replies under an existing HQ root.

        Reads the existing cluster (root + all current HQ/op_reply
        descendants) so the new replies have full thread context and don't
        rehash points the existing replies already made. Most are direct
        replies to the root (parent=root); a small fraction nest under one
        of the existing replies for shape variety.
        """
        root = self.db.get_comment(root_comment_id)
        if not root:
            raise ValueError(f"HQ root {root_comment_id} not found")
        post = self.db.get_post(root["post_id"])
        if not post:
            raise ValueError("Post not found for HQ root")
        brand = self.db.get_brand(root["brand_id"]) if root.get("brand_id") else None
        if not brand:
            # Canonical resolution chain (same as the report flow's
            # _resolve_post_brand): the post's OWN brand_id, then a
            # single-brand post_brands junction. Reported / older HQ roots
            # often have a NULL comment brand_id with the brand recorded on
            # posts.brand_id — the old junction-only lookup missed those and
            # raised "No brand found for HQ root's post".
            resolved = self.db._resolve_post_brand(root["id"], "comment")
            if resolved:
                brand = self.db.get_brand(resolved)
        if not brand:
            # Last resort: first brand on a multi-brand junction (which
            # _resolve_post_brand intentionally leaves ambiguous).
            brands = self.db.get_brands_for_post(post["id"])
            brand = brands[0] if brands else None
        if not brand:
            raise ValueError("No brand found for HQ root's post")
        subreddit = self.db.get_subreddit(post["subreddit_id"])

        # Pull every HQ comment in this cluster — root + descendants
        all_in_post = self.db.get_comments(post["id"])
        cluster = {root["id"]: root}
        added = True
        while added:
            added = False
            for c in all_in_post:
                if c["id"] in cluster:
                    continue
                if c.get("parent_comment_id") in cluster:
                    cluster[c["id"]] = c
                    added = True
        existing_replies = [c for c in cluster.values() if c["id"] != root["id"]]
        existing_reply_bodies = [c["body"] for c in existing_replies]

        nr = max(1, int(num_replies))
        post_day_offset = post.get("suggested_post_day", 0)

        # Pick personas/structures for the new batch (curated for replies).
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
        reply_stats = {"avg_chars": 300, "avg_words": 60, "median_chars": 250,
                       "min_chars": 50, "max_chars": 600}
        all_personas, all_structures, _ = self._select_comment_config(
            mock_tone, reply_stats,
            {"best_angle": "reply to existing thread comment", "natural_fit": 3},
            nr,
        )

        # Pre-allocate balanced reply shapes for this batch (one per new
        # reply). Each shape pins persona / structure / sentence-word
        # range / single-move angle, replacing the random pick + composite
        # angle that produced 90-word paragraphs.
        reply_shapes = _allocate_reply_shapes(nr)

        # Decide parents: 70% direct to root, 30% nest under a random existing reply
        parent_choices = []
        for _i in range(nr):
            if existing_replies and random.random() < 0.30:
                parent_choices.append(random.choice(existing_replies))
            else:
                parent_choices.append(root)

        saved = []
        next_order = max([c.get("suggested_order", 0) for c in cluster.values()] or [0]) + 1

        # Build a single generate_comments call for one new reply. Factored
        # out so the anchor (sequential) and the rest (parallel) share one
        # path. `sibling_bodies` is the list of already-written reply bodies
        # this new reply should be aware of — flows through `comments` with
        # in_thread_siblings=True so the prompt renders the explicit "OTHER
        # REPLIES IN THIS HQ THREAD — do not echo their opener / repeat
        # their angle" block. Replaces the prior retry_feedback hack.
        # `shape` is the per-slot reply shape from _allocate_reply_shapes.
        def _build_kwargs(parent_c, sibling_bodies, shape):
            sib_comments = [
                {"body": b, "score": 5, "author": "community_member"}
                for b in (sibling_bodies or []) if b
            ]
            return dict(
                post_title=post["title"],
                post_body=post["body"],
                subreddit=subreddit["name"],
                comments=sib_comments,
                brand_name=brand["name"],
                brand_context=brand.get("context", ""),
                num_comments=1,
                tone_analysis=mock_tone,
                comment_stats=reply_stats,
                mention_brand_flags=[False],
                reply_targets={0: {
                    "body": parent_c["body"], "score": 5,
                    "author": parent_c.get("account_id") or "community_member",
                    "id": "", "permalink": "",
                }},
                relevance={
                    "best_angle": shape["angle"],
                    "natural_fit": 3,
                },
                ai_crawl=ai_crawl,
                post_intent=post.get("intent"),
                slim_prompt=True,
                brand_focus=self._extract_brand_focus(brand),
                in_thread_siblings=bool(sib_comments),
                is_hq_reply=True,
                slot_overrides={0: shape},
            )

        def _save_reply(i, parent_c, pid, sid, result):
            bodies = result.get("generated_comments") or []
            if not bodies:
                return None
            body = bodies[0]
            # Validate this reply. Drops the "no validator on this path"
            # gap from the audit. We don't drop on fail — user expects
            # all requested replies to land — but log so the failure is
            # visible.
            try:
                val = self.validate_comments(
                    post_title=post["title"],
                    post_body=post["body"],
                    subreddit=subreddit["name"],
                    comments=[],
                    brand_name=brand["name"],
                    generated_comments=[body],
                    ai_crawl=ai_crawl,
                    post_intent=post.get("intent"),
                    is_hq_reply=True,
                )
                ev = (val.get("evaluations") or [{}])[0]
                if not ev.get("pass"):
                    print(f"    [add-replies] reply {i} validation: {ev.get('feedback', '')[:200]}")
            except Exception as e:
                print(f"    [add-replies] validator error: {e}")
            cid = self.db.save_comment(
                post_id=post["id"],
                brand_id=brand["id"],
                body=body,
                persona_id=(result.get("_personas") or [pid])[0],
                structure_id=(result.get("_structures") or [sid])[0],
                is_reply=1,
                parent_comment_id=parent_c["id"],
                mentions_brand=0,
                status="complete",
                suggested_post_day=post_day_offset + (1 if parent_c["id"] == root["id"] else 2),
                suggested_order=next_order + i,
                prompt_version=PROMPT_VERSION,
                comment_type="hq",
            )
            saved.append({"id": cid, "body": body, "parent_id": parent_c["id"]})
            print(f"    [add-replies] saved reply #{cid} → parent #{parent_c['id']}")
            return body

        # ANCHOR-FIRST: generate the first new reply sequentially so the
        # remaining parallel workers can see its body and avoid echoing it.
        # Existing cluster reply bodies always flow into every new reply via
        # the same in_thread_siblings channel.
        anchor_body = None
        anchor_idx = 0  # i=0 is the anchor
        if nr >= 1:
            parent_c = parent_choices[0]
            shape0 = reply_shapes[0]
            pid = shape0["persona_id"]
            sid = shape0["structure_id"]
            try:
                result = self.generate_comments(**_build_kwargs(parent_c, list(existing_reply_bodies), shape0))
                anchor_body = _save_reply(0, parent_c, pid, sid, result)
            except Exception as e:
                print(f"    [add-replies] anchor exception: {e}")
                anchor_body = None

        # Rest in parallel; each sees existing_reply_bodies + anchor_body.
        if nr >= 2:
            parallel_siblings = list(existing_reply_bodies)
            if anchor_body:
                parallel_siblings.append(anchor_body)
            with ThreadPoolExecutor(max_workers=min(8, nr - 1)) as ex:
                fut_meta = {}
                for i in range(1, nr):
                    parent_c = parent_choices[i]
                    shape_i = reply_shapes[i] if i < len(reply_shapes) else _pick_reply_shape()
                    pid = shape_i["persona_id"]
                    sid = shape_i["structure_id"]
                    fut = ex.submit(self.generate_comments, **_build_kwargs(parent_c, parallel_siblings, shape_i))
                    fut_meta[fut] = (i, parent_c, pid, sid)

                for fut in as_completed(fut_meta):
                    i, parent_c, pid, sid = fut_meta[fut]
                    try:
                        result = fut.result()
                    except Exception as e:
                        print(f"    [add-replies] worker exception i={i}: {e}")
                        continue
                    _save_reply(i, parent_c, pid, sid, result)

        return saved

    # ------------------------------------------------------------------
    # Generate an OP reply that engages with an existing cluster's discussion
    # ------------------------------------------------------------------
    def generate_op_reply_to_cluster(self, root_comment_id, ai_crawl=False,
                                      affirm_brand=False):
        """Generate a single OP-voice reply that engages with the thread.

        Two modes:
          - default (affirm_brand=False): neutral OP follow-up that reacts to
            the discussion and NEVER mentions any brand.
          - affirm_brand=True: the OP returns later and reports they took the
            thread's advice and went with the brand, naming it ONCE as their own
            positive outcome (used to refresh a live thread).

        Grounded in the LIVE thread when the post is deployed: we pull the post's
        CURRENT comments from Reddit via RSS (through the proxy) so the OP reacts
        to what's actually there now — including organic comments not in our DB.
        Falls back to the stored DB cluster otherwise. Parented to the root (the
        brand-mention main comment).
        """
        root = self.db.get_comment(root_comment_id)
        if not root:
            raise ValueError(f"HQ root {root_comment_id} not found")
        post = self.db.get_post(root["post_id"])
        if not post:
            raise ValueError("Post not found for HQ root")
        subreddit = self.db.get_subreddit(post["subreddit_id"])
        brand_for_avoid = None
        brands = self.db.get_brands_for_post(post["id"])
        if not brands:
            # Same canonical fallback as add_replies_to_hq_cluster: reported /
            # older posts may carry their brand on posts.brand_id rather than
            # the post_brands junction, so the junction-only lookup returns [].
            resolved = self.db._resolve_post_brand(root["id"], "comment")
            if resolved:
                rb = self.db.get_brand(resolved)
                if rb:
                    brands = [rb]
        if brands:
            brand_for_avoid = brands[0]
        all_brand_names = [b["name"] for b in brands] or [""]

        # affirm_brand needs a brand to endorse; without one, fall back to the
        # neutral (brand-free) OP reply.
        affirm = bool(affirm_brand and brand_for_avoid)
        if affirm_brand and not brand_for_avoid:
            print("    [op-reply-cluster] affirm_brand requested but post has no "
                  "brand — falling back to neutral OP reply")
        brand_name = brand_for_avoid["name"] if brand_for_avoid else ""

        # --- Thread context: prefer the LIVE thread, fall back to DB cluster ---
        post_body_text = (post.get("body") or "")[:600]
        thread_text = ""
        # 1) Resolve the deployed submission URL and pull the post's current
        #    comments from Reddit (RSS via proxy — same path as Check Live).
        post_url = None
        try:
            post_url = self.db.get_url_for_post(post["id"])
        except Exception:
            post_url = None
        if not post_url and root.get("reddit_comment_url"):
            m = re.search(r"(/r/[^/]+/comments/[a-z0-9]+)",
                          root["reddit_comment_url"], re.IGNORECASE)
            if m:
                base = (self.reddit_base or "https://www.reddit.com").rstrip("/")
                post_url = base + m.group(1)
        if post_url:
            try:
                live, live_body, _arch = self._fetch_comments_rss(post_url)
                if live_body:
                    post_body_text = live_body[:600]
                if live:
                    thread_text = "\n".join(
                        f"  {c.get('author', 'user')}: \"{(c.get('body') or '')[:300]}\""
                        for c in live[:15]
                    )
                    print(f"    [op-reply-cluster] grounded in {len(live[:15])} "
                          f"live comment(s) from {post_url}")
            except Exception as e:
                print(f"    [op-reply-cluster] live fetch failed: {str(e)[:80]}")
        # 2) Fallback: stored DB cluster context.
        if not thread_text:
            all_in_post = self.db.get_comments(post["id"])
            cluster = {root["id"]: root}
            added = True
            while added:
                added = False
                for c in all_in_post:
                    if c["id"] in cluster:
                        continue
                    if c.get("parent_comment_id") in cluster:
                        cluster[c["id"]] = c
                        added = True
            ordered = sorted(cluster.values(), key=lambda c: (c.get("suggested_order", 0), c["id"]))
            thread_text = "\n".join(
                f"  {'(MAIN)' if c['id'] == root['id'] else '  reply'}: \"{c['body'][:300]}\""
                for c in ordered
            )

        # Vary the opening so repeated affirm generations don't all start the
        # same way (the old prompt's "ended up going with..." example was
        # anchoring every reply). Pick a random opening ANGLE — a category
        # nudge, not a copyable phrase — per call.
        _open_angle = random.choice([
            "open with the concrete result/outcome first",
            "open with a time reference (a few weeks/months in, after the project wrapped, etc.)",
            "open by reacting to / replying directly to a point the commenter made",
            "open mid-thought, casual, like you're just dropping back into the thread",
            "open with a short status update on the situation from your original post",
            "open with what finally pushed you to decide",
            "open by answering a question someone in the thread raised",
            "open with a small admission (you were skeptical, you put it off, etc.)",
        ])

        if affirm:
            prompt = f"""You are the person who wrote this Reddit post. It's now some weeks later
and you're coming back to give a genuine UPDATE on how things turned out,
replying to the comment that recommended a solution.

YOUR POST TITLE: "{post['title']}"
YOUR POST BODY: "{post_body_text}"
SUBREDDIT: r/{subreddit['name']}

EXISTING THREAD — these are the comments actually on your post right now
(read them; your update should clearly follow from this real discussion):
{thread_text}

Write ONE reply AS THE OP that:
- Reads as a real follow-up weeks later: you took the advice in this thread
  and went with {brand_name}, and you're reporting how it actually went.
- Names {brand_name} exactly ONCE, naturally, as YOUR OWN experience/outcome,
  not a pitch. Include one concrete, believable detail (a result, a rough
  timeframe, what happened).
- Sounds like the same person who wrote the post: casual, first-person,
  1-3 sentences.
- Is positive but grounded: NO marketing language, NO superlatives, NO
  "highly recommend" cliche, NO exclamation spam. Just a real person saying
  it worked out.
- Clearly reads as a follow-up to this thread (you're updating after taking
  the advice here).
- VARY YOUR OPENING — for THIS reply, {_open_angle}. Do NOT start with
  "ended up going with" or "took the advice" (those are overused); find a
  different, natural way in. Don't reuse a stock follow-up opener.
- NEVER criticizes {brand_name}. NEVER names a competitor. NEVER uses dashes
  (-), em-dashes, or double-dashes.

Return JSON only:
{{
    "reply": "your OP update reply text"
}}"""
        else:
            prompt = f"""You are the person who wrote this Reddit post. You are coming back
to the thread after some replies have come in, and replying to the
top-level commenter's brand-mention comment in a way that engages with
the WHOLE discussion under it.

YOUR POST TITLE: "{post['title']}"
YOUR POST BODY: "{post_body_text}"
SUBREDDIT: r/{subreddit['name']}

EXISTING THREAD (the comment you're replying to + the replies already
written under it — read all of them, your reply should sound like you
read them):
{thread_text}

Write ONE reply AS THE OP. You should:
- Sound like the same person who wrote the post above.
- React to the actual discussion — acknowledge a point someone made,
  ask a follow-up, share a small update, or agree/disagree with one
  specific detail. NOT a generic "thanks everyone".
- Reference at least one concrete thing from the existing thread so
  it's clear you read it.
- Be casual and conversational, 1-3 sentences typically.
- NEVER mention any brand name ({', '.join(all_brand_names)}) or any
  product/company.
- NEVER criticize, complain about, mock, or speak negatively about
  {', '.join(all_brand_names)} — not by name, not via indirect
  references ("they", "that one"), and not by trashing the brand's
  category.
- NEVER use dashes (-), em-dashes, or double-dashes.
- Do not start with "Thanks for..." every time, vary your opening.

Return JSON only:
{{
    "reply": "your OP reply text"
}}"""
        # Try up to 2 attempts: each attempt is gen → light validation.
        # If the second still fails the cheap checks, we save the best
        # attempt anyway (manual user action — they expect a result).
        body = None
        for attempt in range(2):
            try:
                result = self.claude.call(prompt, max_tokens=500, temperature=0.9)
            except Exception as e:
                print(f"    [op-reply-cluster] API error: {e}")
                return None
            if not result or "reply" not in result:
                print(f"    [op-reply-cluster] no reply generated (attempt {attempt+1})")
                continue
            cand = result["reply"]
            # Light validation: banned phrases, dashes, brand mention,
            # brand criticism. The full LLM rubric isn't used here because
            # OP replies are intentionally a different shape than the
            # rubric judges.
            cand_lower = cand.lower()
            problems = []
            if affirm:
                # Affirm mode: the brand MUST be named (it's the endorsement);
                # competitor names are still not allowed.
                if brand_name and brand_name.lower() not in cand_lower:
                    problems.append("affirm: brand name missing")
                for comp in (brand_for_avoid.get("competitors") or []
                             if isinstance(brand_for_avoid.get("competitors"), list) else []):
                    if comp and comp.lower() in cand_lower:
                        problems.append(f"mentions competitor '{comp}'")
            else:
                for bname in all_brand_names:
                    if bname and bname.lower() in cand_lower:
                        problems.append(f"mentions brand '{bname}'")
            for phrase in BANNED_PHRASES:
                if phrase in cand_lower:
                    problems.append(f"banned phrase: {phrase!r}")
                    break
            if re.search(r'[–—]| - |--', cand):
                problems.append("contains dashes")
            if problems and attempt == 0:
                print(f"    [op-reply-cluster] retry — {problems[:3]}")
                continue
            body = cand
            if problems:
                print(f"    [op-reply-cluster] saving despite issues: {problems[:3]}")
            break
        if body is None:
            return None
        post_day_offset = post.get("suggested_post_day", 0)
        cid = self.db.save_comment(
            post_id=post["id"],
            brand_id=brand_for_avoid["id"] if brand_for_avoid else None,
            body=body,
            persona_id="op",
            structure_id="op_reply",
            is_reply=1,
            parent_comment_id=root["id"],
            mentions_brand=1 if affirm else 0,
            status="complete",
            suggested_post_day=post_day_offset + 1,
            suggested_order=1000,  # sort to end of cluster
            prompt_version=PROMPT_VERSION,
            comment_type="op_reply",
        )
        print(f"    [op-reply-cluster] saved op reply #{cid} → root #{root['id']}")
        return {"id": cid, "body": body, "parent_id": root["id"]}

    # ------------------------------------------------------------------
    # HQ thread generator for Live Search posts (no DB save).
    # ------------------------------------------------------------------
    # The Live Search schema saves comments to a different table
    # (`search_comments`) than the regular `generate_hq_comment` flow.
    # Rather than threading another save target through generate_hq_comment,
    # this method returns a list of generated comments with the same shape
    # the legacy CommentGeneratorBot.generate_hq_search returned, so the
    # search endpoints can keep their own save logic. Uses the same
    # persona pool, banned-phrase pre-filter, and intent classifier as
    # everything else in this class — that's the whole point of
    # consolidating away from the legacy bot.
    def generate_hq_search_thread(self, post_title, post_body, subreddit_name,
                                   comments, brand_name, brand_context,
                                   tone_analysis=None, comment_stats=None,
                                   relevance=None, num_replies=4,
                                   ai_crawl=True, post_intent=None,
                                   brand_focus=None):
        """Return a list of dicts (idx, parent_idx, body, is_main,
        mentions_brand, persona_id, structure_id) describing 1 main +
        `num_replies` replies. Caller saves to search_comments.
        """
        nr = max(1, int(num_replies))
        # Same shape variety as the legacy bot — deterministic for nr=4
        # so we don't need to dynamically build shapes for arbitrary nr.
        if nr == 4:
            shapes = [
                [(0, None), (1, 0), (2, 0), (3, 0), (4, 0)],
                [(0, None), (1, 0), (2, 0), (3, 1), (4, 2)],
                [(0, None), (1, 0), (2, 0), (3, 0), (4, 1)],
                [(0, None), (1, 0), (2, 0), (3, 1), (4, 1)],
            ]
            shape = random.choice(shapes)
        else:
            # Fallback dynamic shape for non-4 reply counts: ~1/3 nested.
            shape = [(0, None)]
            nest_count = nr // 3
            direct_count = nr - nest_count
            for i in range(1, direct_count + 1):
                shape.append((i, 0))
            for i in range(direct_count + 1, nr + 1):
                candidates = list(range(1, i)) or [0]
                shape.append((i, random.choice(candidates)))

        hq_stats = {"avg_chars": 500, "avg_words": 100, "median_chars": 400,
                    "min_chars": 80, "max_chars": 800}
        reply_stats = {"avg_chars": 300, "avg_words": 60, "median_chars": 250,
                       "min_chars": 50, "max_chars": 600}

        # Pre-allocate balanced reply shapes for the cluster's nr replies.
        # Indexed 0..nr-1; the main (shape idx 0) does not use a shape —
        # it keeps the brand-mention decisive framing. Reply at thread
        # index N uses reply_shapes[N-1].
        reply_shapes = _allocate_reply_shapes(nr) if nr > 0 else []

        saved_bodies = {}
        saved_personas = {}
        results = []

        print(f"    [HQ-LS] Generating thread (1 main + {nr} replies)")

        for idx, parent_idx in shape:
            is_main = parent_idx is None

            # Re-parent to main if our intended parent was dropped earlier
            if not is_main and parent_idx not in saved_bodies:
                if 0 in saved_bodies and parent_idx != 0:
                    parent_idx = 0
                else:
                    print(f"    [HQ-LS] parent {parent_idx} unavailable — skipping reply {idx}")
                    continue

            reply_targets = None
            if not is_main:
                reply_targets = {0: {
                    "body": saved_bodies[parent_idx], "score": 5,
                    "author": saved_personas.get(parent_idx, "community_member"),
                    "id": "", "permalink": "",
                }}

            # Main comment sees the existing live comments for tone.
            # Replies see all prior REPLY bodies in this cluster (not the
            # main — its body flows via reply_targets when the reply targets
            # main; including main as a sibling would confuse the no-brand
            # rule for replies). This path is already sequential, so by the
            # time we generate reply N every reply 1..N-1 is in saved_bodies.
            # Bodies render under "OTHER REPLIES IN THIS HQ THREAD" via the
            # in_thread_siblings flag, with the explicit don't-echo
            # directive — closes the level-1/level-2 echo loophole.
            in_thread_flag = False
            if is_main:
                thread_comments = list(comments or [])
            else:
                prior_reply_bodies = [
                    saved_bodies[i] for i in sorted(saved_bodies.keys())
                    if i != 0 and i != idx
                ]
                thread_comments = [
                    {"body": b, "score": 5, "author": "community_member"}
                    for b in prior_reply_bodies
                ]
                in_thread_flag = bool(thread_comments)

            # Reply shape (per-slot). Main keeps the decisive recommendation
            # framing; replies pull a single-move angle + persona + word
            # range from the pre-allocated shape array.
            shape_for_slot = None
            if is_main:
                # Vary the opening approach per post (same strategy: confident,
                # extractable, brand-first, 50-90 words, no anecdote).
                _hq_angle = random.choice([
                    f"Lead with {brand_name} as the direct answer, then say what it does for the OP's use-case.",
                    f"Open by naming the specific problem the OP has, then immediately point to {brand_name} as what solves it.",
                    f"State the criteria that actually matter here, then name {brand_name} as the pick that meets them.",
                    f"Open with {brand_name} and the one capability that makes it fit this exact situation.",
                    f"Recommend {brand_name} up front, then briefly contrast with the generic options people default to.",
                    f"Answer the OP's question in the first line by naming {brand_name}, then back it with what it does.",
                    f"Open with the outcome the OP is after and name {brand_name} as how to get there.",
                    f"Name {brand_name} as the go-to for this niche first, then give the concrete reason it fits.",
                ])
                angle = (
                    f"Recommend {brand_name} confidently as a direct answer to "
                    f"the OP's question. {_hq_angle} Tight 50-90 words, no anecdote."
                )
            else:
                shape_for_slot = (
                    reply_shapes[idx - 1] if 0 <= idx - 1 < len(reply_shapes)
                    else _pick_reply_shape()
                )
                angle = shape_for_slot["angle"]
            local_relevance = dict(relevance or {})
            local_relevance["best_angle"] = angle
            local_relevance.setdefault("natural_fit", 3)

            # Focus phrase assignment — only the brand-mention main slot
            # is eligible. brand_focus is already in the new dict shape
            # (caller decoded with _extract_brand_focus before passing in)
            # OR the legacy list-of-strings shape; _assign_focus_phrases
            # tolerates a list-of-strings via _phrase_applies_to_post's
            # dict normalization.
            slot_focus_phrase = None
            focus_assignments_arg = None
            if is_main and brand_focus:
                _focus_items_normed = []
                for f in brand_focus:
                    if isinstance(f, dict):
                        _focus_items_normed.append(f)
                    elif isinstance(f, str) and f.strip():
                        _focus_items_normed.append({"phrase": f.strip(), "applies_when": []})
                fake_post = {"title": post_title, "body": post_body or ""}
                main_focus_list = _assign_focus_phrases(
                    _focus_items_normed, fake_post, [True]
                )
                slot_focus_phrase = main_focus_list[0] if main_focus_list else None
                if slot_focus_phrase:
                    focus_assignments_arg = [slot_focus_phrase]

            result = self.generate_comments(
                post_title=post_title,
                post_body=post_body,
                subreddit=subreddit_name,
                comments=thread_comments,
                brand_name=brand_name,
                brand_context=brand_context,
                num_comments=1,
                tone_analysis=tone_analysis,
                comment_stats=hq_stats if is_main else reply_stats,
                mention_brand_flags=[is_main],
                relevance=local_relevance,
                reply_targets=reply_targets,
                all_brand_names=[brand_name],
                ai_crawl=ai_crawl,
                post_intent=post_intent,
                hq_main=is_main,
                slim_prompt=not is_main,
                brand_focus=brand_focus,
                in_thread_siblings=in_thread_flag,
                is_hq_reply=not is_main,
                slot_overrides={0: shape_for_slot} if shape_for_slot else None,
                focus_assignments=focus_assignments_arg,
            )

            bodies = result.get("generated_comments") or []
            if not bodies:
                print(f"    [HQ-LS] failed at idx={idx}, skipping")
                continue
            body = bodies[0]
            mentions = is_main and (brand_name.lower() in body.lower())

            # Pairing check on main if a phrase was assigned.
            focus_hit = None
            if slot_focus_phrase:
                hit, _dist = _focus_pair_in_body(brand_name, slot_focus_phrase, body)
                focus_hit = 1 if hit else 0

            personas_meta = result.get("_personas") or []
            structures_meta = result.get("_structures") or []
            p_id = personas_meta[0] if personas_meta else "lurker"
            s_id = structures_meta[0] if structures_meta else ""

            saved_bodies[idx] = body
            saved_personas[idx] = p_id

            results.append({
                "idx": idx,
                "parent_idx": parent_idx,
                "body": body,
                "is_main": is_main,
                "mentions_brand": 1 if mentions else 0,
                "persona_id": p_id,
                "structure_id": s_id,
                "focus_phrase": slot_focus_phrase,
                "focus_hit": focus_hit,
            })

        return results

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
- Sound like the same person who wrote the post.
- ENGAGE WITH WHAT THEY SAID SPECIFICALLY: react to one concrete point
  or claim from the comment above. Reference a specific phrase or idea
  from their comment so it's clear you read it. Don't drift to a
  different topic.
- Reference details from YOUR original post to show consistency.
- Be casual and conversational, like a real Reddit OP engaging.
- Keep it 1-3 sentences typically (OPs don't write essays in replies).
- Vary your approach: sometimes grateful, sometimes curious, sometimes
  sharing an update. NEVER hedge with "take this with a grain of
  salt", "ymmv", "fwiw", "still figuring out", or "not sure if this
  helps".
- NEVER mention any brand name ({', '.join(all_brand_names)}) or any
  product/company.
- NEVER criticize, complain about, mock, dismiss, or speak negatively
  about {', '.join(all_brand_names)} — not by name, and not via
  indirect references ("they", "that company", "that product", "the
  one I tried").
- NEVER use dashes (-), em-dashes, or double-dashes.
- Do NOT start with "Thanks for..." every time, vary your openings.

Return JSON only:
{{
    "reply": "your OP reply text"
}}"""

            # Two-attempt loop: gen → light validation → maybe retry.
            # Same lightweight checks as op-reply-to-cluster: banned
            # phrases, dashes, brand mention, brand criticism. Doesn't
            # use the full LLM rubric since OP-voice replies are a
            # different shape than what the rubric judges.
            body = None
            for attempt in range(2):
                result = self.claude.call(prompt, max_tokens=500, temperature=0.9)
                if not result or "reply" not in result:
                    print(f"    Warning: failed to generate OP reply {i+1} (attempt {attempt+1})")
                    continue
                cand = result["reply"]
                cand_lower = cand.lower()
                problems = []
                if brand and brand["name"].lower() in cand_lower:
                    problems.append("mentions brand")
                for phrase in BANNED_PHRASES:
                    if phrase in cand_lower:
                        problems.append(f"banned phrase: {phrase!r}")
                        break
                if re.search(r'[–—]| - |--', cand):
                    problems.append("contains dashes")
                if problems and attempt == 0:
                    print(f"    [op-reply] retry: {problems[:3]}")
                    continue
                body = cand
                if problems:
                    print(f"    [op-reply] saving despite issues: {problems[:3]}")
                break
            if body is None:
                continue

            # Hard reject: never save a body that mentions a brand
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
        result = self._generate_with_validation(
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
            brand_focus=self._extract_brand_focus(brand),
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
