"""Brand enrichment: fetch homepage + ask Claude to extract GEO-relevant structured fields.

Used by the /api/brands/enrich routes and by CLI flows. Returns a draft dict that the
user reviews in the UI before saving. Never persists directly.
"""

import json
import re
from html.parser import HTMLParser

import requests

from config import REDDIT_USER_AGENT
from generators.base import ClaudeClient


class _VisibleTextExtractor(HTMLParser):
    """Stdlib-only HTML text extractor — strips scripts/styles, keeps visible content."""

    _SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe"}

    def __init__(self):
        super().__init__()
        self._buf = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._buf.append(text)

    def text(self):
        joined = " ".join(self._buf)
        # Collapse repeated whitespace
        return re.sub(r"\s+", " ", joined).strip()


def _fetch_homepage(domain_url: str, timeout: int = 10) -> str:
    """Fetch a brand's homepage HTML. Returns empty string on any failure."""
    if not domain_url:
        return ""
    url = domain_url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": REDDIT_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=timeout,
            allow_redirects=True,
        )
        if resp.status_code == 200 and resp.text:
            return resp.text
    except requests.exceptions.RequestException as e:
        print(f"[brand_enrichment] fetch error for {url}: {e}")
    return ""


def _extract_visible_text(html: str, max_chars: int = 6000) -> str:
    """Strip HTML tags and return the visible text, capped at max_chars."""
    if not html:
        return ""
    parser = _VisibleTextExtractor()
    try:
        parser.feed(html)
    except Exception as e:
        print(f"[brand_enrichment] parse error: {e}")
        return ""
    return parser.text()[:max_chars]


def _build_enrichment_prompt(name: str, domain_url: str, page_text: str) -> str:
    if page_text:
        page_section = f'HOMEPAGE TEXT (visible content only):\n"""\n{page_text}\n"""'
    else:
        page_section = (
            "HOMEPAGE TEXT: (could not fetch homepage — rely on the brand name and "
            "your general knowledge; flag uncertainty by leaving fields as empty strings "
            "or empty arrays.)"
        )

    return f"""You are analyzing a brand to extract structured context for a GEO (Generative
Engine Optimization) content strategy. The goal is to write Reddit posts that mirror
the long-tail questions real users type into ChatGPT/Perplexity about this brand's
domain — WITHOUT naming the brand itself.

BRAND NAME: {name}
BRAND URL: {domain_url or "(none)"}

{page_section}

Extract the following fields. Be specific and concrete — vague answers are useless.

- category: A precise product category, 3-8 words. Example: "project management SaaS for remote teams", "direct-to-consumer electric toothbrush". Not "software" or "product".
- audience: The ideal customer profile (ICP). Who buys/uses this? Role, team size, industry, context. 1-2 sentences.
- use_cases: 4-6 concrete jobs-to-be-done — what problems do users hire this product to solve? Each item should be a short phrase ("running async standups across timezones"), not a sentence.
- pain_points: 4-6 concrete pains the product addresses. Each item is a phrase ("tickets get lost between Slack and Jira"). These are the pains real users would complain about on Reddit.
- features: 4-6 key differentiating features or capabilities. Each is a short phrase.
- competitors: 3-8 direct competitor brand/product NAMES (real names like "Notion", "Asana", "Linear"). These will be used in comparison-intent posts, so accuracy matters. If you're unsure, include fewer but only confident ones.
- context_summary: A 2-3 sentence narrative describing what the brand is and who it serves. This replaces or augments the existing brand context field.

Return JSON only, exactly this shape:
{{
  "category": "string",
  "audience": "string",
  "use_cases": ["string", ...],
  "pain_points": ["string", ...],
  "features": ["string", ...],
  "competitors": ["string", ...],
  "context_summary": "string"
}}"""


def enrich_brand(claude: ClaudeClient, name: str, domain_url: str) -> dict:
    """Fetch homepage + ask Claude to extract the 7 enrichment fields.

    Returns a dict with keys: category, audience, use_cases, pain_points,
    features, competitors, context_summary. On total failure returns an empty
    dict (caller should treat as error).
    """
    html = _fetch_homepage(domain_url)
    page_text = _extract_visible_text(html)
    prompt = _build_enrichment_prompt(name, domain_url, page_text)
    result = claude.call(prompt, max_tokens=1500, temperature=0.3)
    if not isinstance(result, dict):
        return {}

    # Normalize: coerce list fields to lists, string fields to strings
    def _as_list(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    def _as_str(v):
        return str(v).strip() if v else ""

    return {
        "category":        _as_str(result.get("category")),
        "audience":        _as_str(result.get("audience")),
        "use_cases":       _as_list(result.get("use_cases")),
        "pain_points":     _as_list(result.get("pain_points")),
        "features":        _as_list(result.get("features")),
        "competitors":     _as_list(result.get("competitors")),
        "context_summary": _as_str(result.get("context_summary")),
        "_page_fetched":   bool(page_text),
    }


def enrich_brand_for_anchor(claude: ClaudeClient, name: str, domain_url: str, anchor: str) -> dict:
    """Anchor-scoped grounding: what does THIS brand actually offer/do for a given
    topic (the cluster's seed/anchor, e.g. "Longevity")? Grounds in the brand's OWN
    homepage + the model's confident knowledge — NOT the open web — so it never
    invents offerings. When there's no real offering, returns covers=False.

    Returns {summary, covers, key_points} (summary "" / covers False / [] on failure).
    Used on cluster creation to ground the fan-out + post generation in the brand's
    real capability for that anchor (and to flag weak-fit anchors).
    """
    anchor = (anchor or "").strip()
    if not anchor:
        return {"summary": "", "covers": False, "key_points": []}
    html = _fetch_homepage(domain_url)
    page_text = _extract_visible_text(html)
    if page_text:
        page_section = f'HOMEPAGE TEXT (visible content only):\n"""\n{page_text}\n"""'
    else:
        page_section = (
            "HOMEPAGE TEXT: (could not fetch — rely on the brand name and your "
            "confident knowledge of this brand; if unsure, set covers=false rather "
            "than guessing.)"
        )
    prompt = f"""You are grounding a GEO campaign. We are about to build content anchored on a
specific TOPIC for this brand, and need to know what THIS brand actually offers or
does that is relevant to that topic — so the content stays truthful and on-target.

BRAND NAME: {name}
BRAND URL: {domain_url or "(none)"}
ANCHOR TOPIC: "{anchor}"

{page_section}

Describe ONLY what is supported by the page above or your CONFIDENT knowledge of this
brand — do NOT invent products, services, or claims. If the brand has no real offering
relevant to "{anchor}", say so (covers=false): it's better to flag a poor fit than to
fabricate.

Return JSON only, exactly this shape:
{{
  "summary": "1-3 sentences: what this brand specifically offers/does for \\"{anchor}\\" (or, if covers=false, a one-line note that it doesn't really serve this topic)",
  "covers": true or false,
  "key_points": ["concrete offering / service / capability relevant to the topic", "..."]
}}"""
    result = claude.call(prompt, max_tokens=600, temperature=0.2)
    if not isinstance(result, dict):
        return {"summary": "", "covers": False, "key_points": []}
    kp = result.get("key_points")
    if isinstance(kp, list):
        kp = [str(x).strip() for x in kp if str(x).strip()]
    else:
        kp = []
    return {
        "summary": str(result.get("summary") or "").strip(),
        "covers": bool(result.get("covers")),
        "key_points": kp,
    }


def generate_brand_personas(claude: ClaudeClient, name: str, domain_url: str,
                            category: str = "", audience: str = "",
                            use_cases=None, pain_points=None) -> list:
    """Auto-generate a small set of well-designed buyer PERSONAS (ICPs) for this brand,
    grounded in the brand's own site + enrichment. Used for persona-aware fan-out:
    each persona is a distinct kind of asker, with a brand-FIT judgment so the fan-out
    can target only the personas this brand can credibly be the answer for.

    Returns a list (3-5) of:
      {label, profile, trigger, goal, constraints, vocab, fit}
    where fit ∈ {"yes","maybe","no"}. Returns [] on failure. No fabrication — when
    unsure whether the brand serves a persona, mark fit "no" rather than inventing.
    """
    def _join(v):
        if isinstance(v, list):
            return "; ".join(str(x).strip() for x in v if str(x).strip())
        return str(v or "").strip()
    html = _fetch_homepage(domain_url)
    page_text = _extract_visible_text(html, max_chars=4000)
    page_section = (f'HOMEPAGE TEXT (visible content only):\n"""\n{page_text}\n"""'
                    if page_text else
                    "HOMEPAGE TEXT: (could not fetch — rely on the fields below + your "
                    "confident knowledge of this brand.)")
    prompt = f"""You are defining the buyer PERSONAS for a GEO campaign — the distinct kinds of
people who would search for what this brand offers. These personas decide which
recommendation questions are worth targeting and whether THIS brand is a credible
answer for each.

BRAND NAME: {name}
BRAND URL: {domain_url or "(none)"}
CATEGORY: {category or "(unknown)"}
AUDIENCE: {audience or "(unknown)"}
USE-CASES: {_join(use_cases) or "(unknown)"}
PAIN-POINTS: {_join(pain_points) or "(unknown)"}

{page_section}

Produce 4-6 DISTINCT, non-overlapping personas (different situations/intents — not
rewordings of each other). Ground them in the brand's real space; do NOT invent.
For each, judge FIT honestly: would a helpful AI, answering THIS persona's questions,
credibly recommend THIS brand?
  - "yes"   = squarely the brand's customer
  - "maybe" = plausible/adjacent
  - "no"    = this persona wants something the brand isn't (include a couple of these
              when they're realistic — they are the winnability filter; better to flag
              a mismatch than pretend)

Return JSON only, exactly this shape:
{{
  "personas": [
    {{
      "label": "2-4 word name (e.g. 'burnt-out exec')",
      "profile": "who they are + their situation, 1 line",
      "trigger": "what makes them go search",
      "goal": "the job-to-be-done they want solved",
      "constraints": "budget / compliance / urgency / discretion etc. (short)",
      "vocab": "how THEY would phrase it (a few words/terms)",
      "fit": "yes" | "maybe" | "no"
    }}
  ]
}}"""
    result = claude.call(prompt, max_tokens=1400, temperature=0.4)
    items = (result or {}).get("personas") if isinstance(result, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for p in items:
        if not isinstance(p, dict):
            continue
        label = str(p.get("label") or "").strip()
        if not label:
            continue
        fit = str(p.get("fit") or "").strip().lower()
        if fit not in ("yes", "maybe", "no"):
            fit = "maybe"
        out.append({
            "label": label,
            "profile": str(p.get("profile") or "").strip(),
            "trigger": str(p.get("trigger") or "").strip(),
            "goal": str(p.get("goal") or "").strip(),
            "constraints": str(p.get("constraints") or "").strip(),
            "vocab": str(p.get("vocab") or "").strip(),
            "fit": fit,
        })
    return out[:6]


def generate_personas_for_regions(claude: ClaudeClient, name: str, domain_url: str,
                                  category: str = "", audience: str = "",
                                  use_cases=None, pain_points=None,
                                  region_queries=None, existing_labels=None) -> dict:
    """Grow the brand's persona roster to cover SEARCH REGIONS that no existing persona fits.

    Given a list of region queries (e.g. "affordable side sleeper mattress under $1000") that
    the current roster can't credibly answer, propose 1-3 NEW, distinct personas (same schema
    as generate_brand_personas, all fit="yes" since they are built for these regions) and map
    each unmatched region to the new persona that fits it.

    Returns {"new_personas": [ {label, profile, trigger, goal, constraints, vocab, fit}, ... ],
             "assignments": [ [region_query, persona_label], ... ]}.
    Returns {"new_personas": [], "assignments": []} on failure — caller leaves those regions
    persona-less rather than forcing a wrong match. No fabrication beyond what the brand
    credibly serves."""
    def _join(v):
        if isinstance(v, list):
            return "; ".join(str(x).strip() for x in v if str(x).strip())
        return str(v or "").strip()
    regions = [str(q).strip() for q in (region_queries or []) if str(q).strip()]
    if not regions:
        return {"new_personas": [], "assignments": []}
    existing = [str(l).strip() for l in (existing_labels or []) if str(l).strip()]
    existing_lower = {l.lower() for l in existing}
    html = _fetch_homepage(domain_url)
    page_text = _extract_visible_text(html, max_chars=4000)
    page_section = (f'HOMEPAGE TEXT (visible content only):\n"""\n{page_text}\n"""'
                    if page_text else
                    "HOMEPAGE TEXT: (could not fetch — rely on the fields below + your "
                    "confident knowledge of this brand.)")
    rlist = "\n".join(f"  {i+1}. {q}" for i, q in enumerate(regions))
    existing_block = ("\n".join(f"  - {l}" for l in existing)
                      if existing else "  (none yet)")
    prompt = f"""These SEARCH REGIONS (recommendation questions) have NO fitting buyer persona in
the brand's current roster — every existing persona is the wrong price tier / intent / use-case
for them. Your job: define the NEW personas that genuinely DO ask these questions, so the brand's
persona set grows to cover its real search demand.

BRAND NAME: {name}
BRAND URL: {domain_url or "(none)"}
CATEGORY: {category or "(unknown)"}
AUDIENCE: {audience or "(unknown)"}
USE-CASES: {_join(use_cases) or "(unknown)"}
PAIN-POINTS: {_join(pain_points) or "(unknown)"}

{page_section}

EXISTING PERSONAS (do NOT duplicate or reword these labels):
{existing_block}

UNCOVERED REGIONS:
{rlist}

Propose 1-3 NEW, DISTINCT personas (fewer is better — only as many as truly needed) that the
brand can credibly be the answer for, each matching the concrete signals in these regions:
price tier (budget/affordable/"under $X" vs premium/luxury), and any position / firmness /
use-case / body-type / urgency cues. Ground them in the brand's real space — do NOT invent a
persona the brand cannot serve; if a region is genuinely off-brand, simply leave it unmapped.
Then map EACH region above to the one new persona that best fits it (omit a region if none of
your new personas fit).

Return JSON only, exactly this shape:
{{
  "new_personas": [
    {{
      "label": "2-4 word name, distinct from existing",
      "profile": "who they are + situation, 1 line",
      "trigger": "what makes them search",
      "goal": "the job-to-be-done",
      "constraints": "budget / urgency / firmness etc. (short)",
      "vocab": "how THEY phrase it (a few terms)",
      "fit": "yes"
    }}
  ],
  "assignments": [ ["<region text, copied exactly from the list>", "<new persona label>"] ]
}}"""
    result = claude.call(prompt, max_tokens=1400, temperature=0.4)
    if not isinstance(result, dict):
        return {"new_personas": [], "assignments": []}
    raw_personas = result.get("new_personas") if isinstance(result.get("new_personas"), list) else []
    new_personas = []
    new_lower = set()
    for p in raw_personas:
        if not isinstance(p, dict):
            continue
        label = str(p.get("label") or "").strip()
        ll = label.lower()
        if not label or ll in existing_lower or ll in new_lower:
            continue  # skip blanks + duplicates of existing/just-added labels
        new_lower.add(ll)
        new_personas.append({
            "label": label,
            "profile": str(p.get("profile") or "").strip(),
            "trigger": str(p.get("trigger") or "").strip(),
            "goal": str(p.get("goal") or "").strip(),
            "constraints": str(p.get("constraints") or "").strip(),
            "vocab": str(p.get("vocab") or "").strip(),
            "fit": "yes",
        })
    new_personas = new_personas[:3]
    valid_labels = {p["label"].lower(): p["label"] for p in new_personas}
    region_lower = {q.lower(): q for q in regions}
    assignments = []
    seen_regions = set()
    raw_assign = result.get("assignments") if isinstance(result.get("assignments"), list) else []
    for a in raw_assign:
        if not isinstance(a, (list, tuple)) or len(a) < 2:
            continue
        rq = str(a[0]).strip()
        lbl = str(a[1]).strip().lower()
        canon_region = region_lower.get(rq.lower())
        if canon_region and lbl in valid_labels and canon_region not in seen_regions:
            assignments.append([canon_region, valid_labels[lbl]])
            seen_regions.add(canon_region)
    return {"new_personas": new_personas, "assignments": assignments}
