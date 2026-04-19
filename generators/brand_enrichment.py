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


def _build_enrichment_prompt(name: str, domain_url: str, page_text: str, existing_context: str = "") -> str:
    if page_text:
        page_section = f'HOMEPAGE TEXT (visible content only):\n"""\n{page_text}\n"""'
    else:
        page_section = (
            "HOMEPAGE TEXT: (could not fetch homepage — rely on the brand name and "
            "your general knowledge; flag uncertainty by leaving fields as empty strings "
            "or empty arrays.)"
        )

    context_section = ""
    if existing_context and existing_context.strip():
        context_section = (
            f'\nUSER-PROVIDED BRAND CONTEXT (authoritative — anything stated here overrides '
            f'inference from the homepage):\n"""\n{existing_context.strip()}\n"""\n'
        )

    return f"""You are analyzing a brand to extract structured context for a GEO (Generative
Engine Optimization) content strategy. The goal is to write Reddit posts that mirror
the long-tail questions real users type into ChatGPT/Perplexity about this brand's
domain — WITHOUT naming the brand itself.

BRAND NAME: {name}
BRAND URL: {domain_url or "(none)"}
{context_section}
{page_section}

Extract the following fields. Be specific and concrete — vague answers are useless.

- category: A precise product category, 3-8 words. Example: "project management SaaS for remote teams", "direct-to-consumer electric toothbrush". Not "software" or "product".
- audience: The ideal customer profile (ICP). Who buys/uses this? Role, team size, industry, context. 1-2 sentences.
- use_cases: 4-6 concrete jobs-to-be-done — what problems do users hire this product to solve? Each item should be a short phrase ("running async standups across timezones"), not a sentence.
- pain_points: 4-6 concrete pains the product addresses. Each item is a phrase ("tickets get lost between Slack and Jira"). These are the pains real users would complain about on Reddit.
- features: 4-6 key differentiating features or capabilities. Each is a short phrase.
- competitors: 3-8 direct competitor brand/product NAMES (real names like "Notion", "Asana", "Linear"). These will be used in comparison-intent posts, so accuracy matters. If you're unsure, include fewer but only confident ones.
- context_summary: A 2-3 sentence narrative describing what the brand is and who it serves. This replaces or augments the existing brand context field.
- service_location: The geographic area the brand serves, IF it is a location-specific/regional brand. Use a concise phrase like "Dallas, TX", "UK only", "California Bay Area", or "Mumbai, India". Return an empty string "" if the brand serves globally/internationally, nationwide in its home country without further restriction, or neither the homepage nor the user-provided context specifies a geographic limit. Check BOTH the user-provided context AND the homepage — if either explicitly names a local/regional service area, fill it in. Do NOT guess.

Return JSON only, exactly this shape:
{{
  "category": "string",
  "audience": "string",
  "use_cases": ["string", ...],
  "pain_points": ["string", ...],
  "features": ["string", ...],
  "competitors": ["string", ...],
  "context_summary": "string",
  "service_location": "string"
}}"""


def enrich_brand(claude: ClaudeClient, name: str, domain_url: str, existing_context: str = "") -> dict:
    """Fetch homepage + ask Claude to extract the enrichment fields.

    `existing_context` is the user-provided free-text "what the brand does"
    description. If supplied, it is included in the LLM prompt as authoritative
    input so location and other fields can be pulled from it too, not just the
    homepage.

    Returns a dict with keys: category, audience, use_cases, pain_points,
    features, competitors, context_summary, service_location. On total failure
    returns an empty dict (caller should treat as error).
    """
    html = _fetch_homepage(domain_url)
    page_text = _extract_visible_text(html)
    prompt = _build_enrichment_prompt(name, domain_url, page_text, existing_context)
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
        "category":         _as_str(result.get("category")),
        "audience":         _as_str(result.get("audience")),
        "use_cases":        _as_list(result.get("use_cases")),
        "pain_points":      _as_list(result.get("pain_points")),
        "features":         _as_list(result.get("features")),
        "competitors":      _as_list(result.get("competitors")),
        "context_summary":  _as_str(result.get("context_summary")),
        "service_location": _as_str(result.get("service_location")),
        "_page_fetched":    bool(page_text),
    }


def extract_service_location(claude: ClaudeClient, name: str, context: str,
                              domain_url: str = "") -> str:
    """Lightweight single-field extraction: pull the brand's service location
    out of its free-text context + (optionally) homepage. Returns "" for
    global brands or when no geographic area is explicitly mentioned.

    Faster + cheaper than full enrichment — used by the "Extract from context"
    button next to the Service Location input.
    """
    if not (context and context.strip()) and not domain_url:
        return ""

    page_text = ""
    if domain_url:
        html = _fetch_homepage(domain_url)
        page_text = _extract_visible_text(html, max_chars=3000)

    context_block = ""
    if context and context.strip():
        context_block = (
            f'\nBRAND CONTEXT (user-provided — authoritative):\n"""\n{context.strip()}\n"""\n'
        )
    page_block = ""
    if page_text:
        page_block = f'\nHOMEPAGE TEXT:\n"""\n{page_text}\n"""\n'

    prompt = f"""Extract the GEOGRAPHIC SERVICE AREA for this brand if it is a location-specific
or regional business. Otherwise return an empty string.

BRAND NAME: {name}
{context_block}{page_block}

Rules:
- If the brand explicitly serves a specific city, state, region, country, or named area,
  return that as a concise phrase (e.g. "Dallas, TX", "UK only", "California Bay Area").
- If the brand serves globally, internationally, nationwide without restriction, or the
  inputs do not mention a specific geographic area, return an empty string "".
- Do NOT guess based on where the company is headquartered. Only return a location if the
  inputs state the service area is limited to that location.

Return JSON only:
{{"service_location": "string"}}"""

    result = claude.call(prompt, max_tokens=200, temperature=0.2)
    if not isinstance(result, dict):
        return ""
    value = result.get("service_location", "")
    return str(value).strip() if value else ""
