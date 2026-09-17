"""Brand enrichment: fetch homepage + ask Claude to extract GEO-relevant structured fields.

Used by the /api/brands/enrich routes and by CLI flows. Returns a draft dict that the
user reviews in the UI before saving. Never persists directly.
"""

import json
import os
import random
import re
import time
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


_CHALLENGE_MARKERS = (
    "just a moment", "attention required", "enable javascript and cookies",
    "checking your browser", "verify you are a human", "access denied",
    "you have been blocked", "captcha", "ddos protection by",
    "cf-browser-verification", "_cf_chl", "cf-challenge",
)


def _looks_blocked(html: str) -> str:
    """FU113 — content gate for a 200 response: bot walls (WPX Cloud, Cloudflare, …)
    serve datacenter IPs a block/challenge page WITH STATUS 200, which used to count
    as a successful fetch — so the residential + web-search fallbacks never fired and
    the model was 'grounded' in "Just a moment…" text (the rantle.com bug). Returns
    "challenge-page" / "thin-content" when the page can't be real grounding, else ""."""
    if not html:
        return "thin-content"
    low = html[:8000].lower()
    for marker in _CHALLENGE_MARKERS:
        if marker in low:
            return "challenge-page"
    # A real company page has real visible text; a JS shell / block stub doesn't.
    if len(_extract_visible_text(html, max_chars=1000)) < 200:
        return "thin-content"
    return ""


def _fetch_homepage(domain_url: str, timeout: int = 10, retries: int = 2) -> str:
    """Fetch a brand's homepage HTML. Returns empty string on any failure.

    Retries a transient failure (connection error / timeout / non-200) with a short
    jittered backoff — on a cloud host (Railway) a single naked GET often hits a
    transient block, and one quick retry recovers it. A hard Cloudflare block stays
    empty (the caller has a web-search fallback for that case)."""
    if not domain_url:
        return ""
    url = domain_url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    # Browser UA (not the Reddit bot UA): some brand sites sit behind Cloudflare which
    # 403s "SubredditStrategyBot/..." — that empty fetch made enrichment fall back to
    # the brand NAME and describe the wrong same-named entity (e.g. landportal.com).
    # Brand-homepage fetch only — unrelated to the Reddit legs.
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    attempts = max(1, int(retries) + 1)
    for i in range(attempts):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200 and resp.text:
                # FU113: a 200 is NOT success unless it carries real content — bot walls
                # serve 200 block/challenge pages, which must fall through the ladder.
                blocked = _looks_blocked(resp.text)
                if not blocked:
                    return resp.text
                print(f"[brand_enrichment] 200-but-blocked ({blocked}) for {url} — "
                      "continuing the ladder", flush=True)
                break   # a bot wall won't change on retry — go straight to residential
        except requests.exceptions.RequestException as e:
            if i == attempts - 1:
                print(f"[brand_enrichment] fetch error for {url}: {e}")
        if i < attempts - 1:
            time.sleep(0.4 + random.uniform(0, 0.4))
    # FU111 — residential-proxy fallback (the rantle.com case): sites that block
    # datacenter IPs return nothing on Railway; the IPRoyal residential IP fetches them
    # fine. SINGLE shot, only after the free path failed (GB is metered — a homepage is
    # ~0.1-0.5 MB and enrichment is rare, so the spend is negligible). The FU110
    # web-search grounding remains the last resort when even this fails.
    proxy = os.environ.get("REDDIT_HTTP_PROXY", "").strip()
    if proxy:
        try:
            resp = requests.get(url, headers=headers, timeout=max(timeout, 12),
                                allow_redirects=True,
                                proxies={"http": proxy, "https": proxy})
            if resp.status_code == 200 and resp.text:
                blocked = _looks_blocked(resp.text)   # FU113: gate the residential rung too
                if not blocked:
                    print(f"[brand_enrichment] ✓ homepage via residential proxy: {url}", flush=True)
                    return resp.text
                print(f"[brand_enrichment] residential fetch 200-but-blocked ({blocked}) "
                      f"for {url}", flush=True)
            else:
                print(f"[brand_enrichment] residential fetch got {resp.status_code} for {url}",
                      flush=True)
        except requests.exceptions.RequestException as e:
            print(f"[brand_enrichment] residential fetch failed for {url}: {e}", flush=True)
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


def _extract_logo_url(html: str, domain_url: str) -> str:
    """Best-effort brand image/logo URL from homepage HTML (no LLM). Prefers og:image,
    then apple-touch-icon / <link rel=icon>, resolved to an absolute URL. "" if none."""
    if not html:
        return ""
    base = (domain_url or "").strip()
    if base and not base.startswith(("http://", "https://")):
        base = "https://" + base
    base = base.rstrip("/")

    def _abs(u):
        u = (u or "").strip()
        if not u:
            return ""
        if u.startswith(("http://", "https://")):
            return u
        if u.startswith("//"):
            return "https:" + u
        if u.startswith("/"):
            return base + u
        return f"{base}/{u}" if base else u

    # 1) og:image / twitter:image (a real share/brand image)
    m = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]*\bcontent=["\']([^"\']+)["\']', html, re.I)
    if not m:
        m = re.search(r'<meta[^>]*\bcontent=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image|twitter:image)["\']', html, re.I)
    if m:
        u = _abs(m.group(1))
        if u:
            return u
    # 2) apple-touch-icon / icon link
    for rel in ("apple-touch-icon", "icon", "shortcut icon"):
        lm = re.search(r'<link[^>]+rel=["\'][^"\']*' + re.escape(rel) + r'[^"\']*["\'][^>]*\bhref=["\']([^"\']+)["\']', html, re.I)
        if not lm:
            lm = re.search(r'<link[^>]*\bhref=["\']([^"\']+)["\'][^>]*rel=["\'][^"\']*' + re.escape(rel) + r'[^"\']*["\']', html, re.I)
        if lm:
            u = _abs(lm.group(1))
            if u:
                return u
    return ""


def _build_enrichment_prompt(name: str, domain_url: str, page_text: str,
                             site_facts: str = "") -> str:
    if page_text:
        page_section = (
            f'HOMEPAGE TEXT (visible content only):\n"""\n{page_text}\n"""\n'
            "Ground EVERY field ONLY in this page text. The brand NAME may collide with "
            "unrelated same-named entities (companies, apps, games) — ignore anything you "
            "associate with the name that is not supported by the page."
        )
    elif site_facts:
        # FU110: the direct fetch failed (datacenter-IP blocks) but web search — which runs
        # server-side, immune to our IP — read the site. Ground in these facts.
        page_section = (
            f'SITE FACTS (gathered via web search of {domain_url}):\n"""\n{site_facts}\n"""\n'
            "Ground EVERY field ONLY in these facts. Ignore any same-named entity that is "
            "not this company at this domain."
        )
    else:
        page_section = (
            "HOMEPAGE TEXT: (could not fetch the site.) The DOMAIN is the identity anchor. "
            "Describe ONLY the company that operates THIS EXACT domain. If you do not have "
            "specific, reliable knowledge of that company, leave fields as empty strings or "
            "empty arrays. NEVER substitute a similarly-named company, app, game, or "
            "organization — name collisions are common and a confident wrong answer is far "
            "worse than an empty one."
        )

    return f"""You are analyzing a brand to extract structured context for a GEO (Generative
Engine Optimization) content strategy. The goal is to write Reddit posts that mirror
the long-tail questions real users type into ChatGPT/Perplexity about this brand's
domain — WITHOUT naming the brand itself.

BRAND NAME: {name or "(unknown — derive the brand's name from the homepage title/logo text or the domain)"}
BRAND URL: {domain_url or "(none)"}

{page_section}

Extract the following fields. Be specific and concrete — vague answers are useless.

- name: The brand's actual name as shown on its site (from the homepage title/logo, or the domain). Short — just the brand name. If a BRAND NAME was already given above, return it unchanged.
- category: A precise product category, 3-8 words. Example: "project management SaaS for remote teams", "direct-to-consumer electric toothbrush". Not "software" or "product".
- audience: The ideal customer profile (ICP). Who buys/uses this? Role, team size, industry, context. 1-2 sentences.
- use_cases: 4-6 concrete jobs-to-be-done — what problems do users hire this product to solve? Each item should be a short phrase ("running async standups across timezones"), not a sentence.
- pain_points: 4-6 concrete pains the product addresses. Each item is a phrase ("tickets get lost between Slack and Jira"). These are the pains real users would complain about on Reddit.
- features: 4-6 key differentiating features or capabilities. Each is a short phrase.
- competitors: 3-8 direct competitor brand/product NAMES (real names like "Notion", "Asana", "Linear"). These will be used in comparison-intent posts, so accuracy matters. If you're unsure, include fewer but only confident ones.
- competitor_domains: an object mapping each competitor NAME above to its official homepage domain (e.g. {{"Notion": "notion.com", "Asana": "asana.com"}}). Include ONLY domains you are confident are the official site; omit a competitor rather than guess. Bare domain, no https://, no path.
- context_summary: A 2-3 sentence narrative describing what the brand is and who it serves. This replaces or augments the existing brand context field.
- keywords: 6-12 concise search keywords/terms a real person would type about this brand's DOMAIN (single words or short 2-3 word phrases like "async standups", "remote team pm"). Do NOT include the brand name. These are used to match relevant Reddit threads, so favor the terms users actually search, not marketing language.

Return JSON only, exactly this shape:
{{
  "name": "string",
  "category": "string",
  "audience": "string",
  "use_cases": ["string", ...],
  "pain_points": ["string", ...],
  "features": ["string", ...],
  "competitors": ["string", ...],
  "competitor_domains": {{"Competitor Name": "domain.com", ...}},
  "context_summary": "string",
  "keywords": ["string", ...]
}}"""


def enrich_brand(claude: ClaudeClient, name: str, domain_url: str) -> dict:
    """Fetch homepage + ask Claude to extract the 7 enrichment fields.

    Returns a dict with keys: category, audience, use_cases, pain_points,
    features, competitors, context_summary. On total failure returns an empty
    dict (caller should treat as error).
    """
    html = _fetch_homepage(domain_url)
    page_text = _extract_visible_text(html)
    # FU113 belt-and-braces: text this thin cannot describe a company — treat it as
    # NO grounding so the web-search fallback fires (bot-wall stubs, JS shells).
    if page_text and len(page_text) < 200:
        print(f"[brand_enrichment] page text too thin ({len(page_text)} chars) — "
              "treating as unfetched", flush=True)
        page_text = ""
    # FU110 — the rantle.com lesson: when the DIRECT fetch fails (sites that block
    # datacenter IPs return nothing on Railway), do NOT fall back to name-recall — a
    # colliding name makes the model confidently describe the WRONG entity (a same-named
    # game, org, …). Instead ground via fetch_site_facts: a web search PINNED to the
    # brand's own domain that runs server-side (immune to our egress IP). ~1-2¢, only
    # on fetch failure. If even that returns nothing, the prompt's hardened no-page
    # branch forbids substituting a similarly-named entity.
    site_facts = ""
    if not page_text and (domain_url or "").strip():
        try:
            _dom = re.sub(r"^https?://", "", domain_url.strip()).split("/")[0]
            _dom = _dom[4:] if _dom.startswith("www.") else _dom
            site_facts = claude.fetch_site_facts(
                _dom, name or _dom,
                "what this company is, what it sells/does, who its customers are, "
                "key products/services, and its main competitors") or ""
            if site_facts:
                print(f"[brand_enrichment] direct fetch of {domain_url} failed — grounded "
                      f"via web-search site facts ({len(site_facts)} chars)", flush=True)
        except Exception as e:
            print(f"[brand_enrichment] site-facts fallback failed: {e}", flush=True)
    # FU113 — grounding visibility: the Railway log now always SHOWS what the model was
    # grounded in, so "why did it describe X" is diagnosable from the log alone.
    _grounding = "homepage" if page_text else ("web_search" if site_facts else "none")
    _preview = (page_text or site_facts or "")[:150].replace("\n", " ")
    print(f"[brand_enrichment] grounding={_grounding} for {domain_url or name} "
          f"preview={_preview!r}", flush=True)
    prompt = _build_enrichment_prompt(name, domain_url, page_text, site_facts=site_facts)
    # 4000 (was 1500): the prompt asks for ~10 fields incl. competitor_domains;
    # 1500 truncated the JSON mid-object -> JSONDecodeError -> None -> "failed".
    result = claude.call(prompt, max_tokens=4000, temperature=0.3)
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

    def _as_domain_map(v):
        """Normalize competitor_domains to {name: bare-domain}. Strips scheme/path,
        drops blanks. Tolerant of a non-dict (returns {})."""
        out = {}
        if isinstance(v, dict):
            for name, dom in v.items():
                n = str(name).strip()
                d = str(dom or "").strip().lower()
                d = re.sub(r"^https?://", "", d).rstrip("/").split("/")[0]
                if n and d:
                    out[n] = d
        return out

    return {
        # Provided name wins; otherwise use the name the model derived from the
        # homepage/domain so URL-only auto-analyze can fill the name field.
        "name":            name or _as_str(result.get("name")),
        "category":        _as_str(result.get("category")),
        "audience":        _as_str(result.get("audience")),
        "use_cases":       _as_list(result.get("use_cases")),
        "pain_points":     _as_list(result.get("pain_points")),
        "features":        _as_list(result.get("features")),
        "competitors":     _as_list(result.get("competitors")),
        "competitor_domains": _as_domain_map(result.get("competitor_domains")),
        "context_summary": _as_str(result.get("context_summary")),
        "keywords":        _as_list(result.get("keywords")),
        "_page_fetched":   bool(page_text),
        "_grounding":      _grounding,   # FU113: homepage | web_search | none — honest toast
    }


# Common pages that name a real founder / owner / team member.
_BYLINE_PATHS = ("", "/about", "/about-us", "/team", "/our-team", "/leadership", "/company")


def fetch_brand_byline_logo(claude: ClaudeClient, name: str, domain_url: str) -> dict:
    """Extract a REAL author (founder / owner / named team member) + title and the brand's
    logo URL from the brand's OWN site. Returns {author_name, author_title, logo_url} (any may
    be "" — never fabricated, never raises). Author is identified by an LLM grounded ONLY in the
    fetched pages; the logo is parsed from homepage HTML (no LLM)."""
    out = {"author_name": "", "author_title": "", "logo_url": ""}
    domain_url = (domain_url or "").strip()
    if not domain_url:
        return out
    base = domain_url if domain_url.startswith(("http://", "https://")) else "https://" + domain_url
    base = base.rstrip("/")
    home_html = ""
    texts = []
    for path in _BYLINE_PATHS:
        html = _fetch_homepage(base + path)
        if not html:
            # FU112: if even the HOMEPAGE ("" — always first in _BYLINE_PATHS) is
            # unreachable, the site is blocked/down — don't burn the full fetch ladder
            # (direct retries + residential shot) on six more paths; that alone could
            # eat minutes and was a big part of blowing the request past the gateway
            # timeout on blocked sites.
            if path == "":
                print(f"[brand_enrichment] byline: homepage unreachable for {base} — "
                      "skipping the remaining byline paths", flush=True)
                break
            continue
        if path == "":
            home_html = html
        txt = _extract_visible_text(html, max_chars=4000)
        if txt:
            texts.append(f"[{path or '/'}] {txt}")
        if len(texts) >= 4:
            break
    # Logo from the homepage HTML (fall back to any fetched page if homepage was blank).
    try:
        out["logo_url"] = _extract_logo_url(home_html, domain_url)
    except Exception as e:
        print(f"[brand_enrichment] logo extract skipped: {e}")
    if not texts:
        return out
    prompt = (
        f'Below is text from the website of "{name or domain_url}". Identify the brand\'s '
        "founder, owner, CEO, or a clearly-named senior team member who could be credited as the "
        "author/reviewer of the brand's articles, and their title/role.\n\n"
        "STRICT RULES:\n"
        "- Return a person ONLY if a real, specific human name is EXPLICITLY stated on these pages.\n"
        "- NEVER invent, guess, or infer a name. If no real named person appears, return empty strings.\n"
        "- Prefer the founder / owner / CEO; otherwise a named senior leader.\n\n"
        "PAGES:\n" + "\n\n".join(texts)[:9000] +
        '\n\nReturn JSON only: {"author_name": "", "author_title": ""}'
    )
    try:
        res = claude.call(prompt, max_tokens=200, temperature=0)
    except Exception as e:
        print(f"[brand_enrichment] byline fetch error: {e}")
        res = None
    if isinstance(res, dict):
        out["author_name"] = str(res.get("author_name") or "").strip()
        out["author_title"] = str(res.get("author_title") or "").strip()
        if not out["author_name"]:          # title without a name is meaningless
            out["author_title"] = ""
    print(f"[brand_enrichment] byline/logo for {name or domain_url}: "
          f"author={out['author_name'] or '(none)'}, logo={'yes' if out['logo_url'] else 'no'}",
          flush=True)
    return out


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
            "HOMEPAGE TEXT: (could not fetch.) Describe ONLY the company at this exact "
            "domain — NEVER a similarly-named company, app, game, or organization (name "
            "collisions are common). If unsure, set covers=false rather than guessing."
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
      {label, profile, trigger, goal, constraints, vocab, pain_points[], use_cases[], fit}
    where fit ∈ {"yes","maybe","no"}. Returns [] on failure. No fabrication — when
    unsure whether the brand serves a persona, mark fit "no" rather than inventing.
    """
    def _join(v):
        if isinstance(v, list):
            return "; ".join(str(x).strip() for x in v if str(x).strip())
        return str(v or "").strip()

    def _aslist(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []
    html = _fetch_homepage(domain_url)
    page_text = _extract_visible_text(html, max_chars=4000)
    page_section = (f'HOMEPAGE TEXT (visible content only):\n"""\n{page_text}\n"""'
                    if page_text else
                    "HOMEPAGE TEXT: (could not fetch — rely ONLY on the fields below; "
                    "NEVER substitute a similarly-named company, app, game, or "
                    "organization for this brand.)")
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
      "pain_points": ["3-5 concrete pains THIS persona specifically feels (short phrases)"],
      "use_cases": ["3-5 jobs-to-be-done THIS persona hires the product for (short phrases)"],
      "fit": "yes" | "maybe" | "no"
    }}
  ]
}}"""
    result = claude.call(prompt, max_tokens=2200, temperature=0.4)
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
            "pain_points": _aslist(p.get("pain_points")),
            "use_cases": _aslist(p.get("use_cases")),
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
                    "HOMEPAGE TEXT: (could not fetch — rely ONLY on the fields below; "
                    "NEVER substitute a similarly-named company, app, game, or "
                    "organization for this brand.)")
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
      "pain_points": ["3-5 concrete pains THIS persona feels (short phrases)"],
      "use_cases": ["3-5 jobs-to-be-done for THIS persona (short phrases)"],
      "fit": "yes"
    }}
  ],
  "assignments": [ ["<region text, copied exactly from the list>", "<new persona label>"] ]
}}"""
    result = claude.call(prompt, max_tokens=2000, temperature=0.4)
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
        def _al(v):
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
            return [str(v).strip()] if (isinstance(v, str) and v.strip()) else []
        new_personas.append({
            "label": label,
            "profile": str(p.get("profile") or "").strip(),
            "trigger": str(p.get("trigger") or "").strip(),
            "goal": str(p.get("goal") or "").strip(),
            "constraints": str(p.get("constraints") or "").strip(),
            "vocab": str(p.get("vocab") or "").strip(),
            "pain_points": _al(p.get("pain_points")),
            "use_cases": _al(p.get("use_cases")),
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


# ─────────────────────────────────────────────────────────────────────────────────────────────
# FU212 — CONTENT INSTRUCTIONS per brand. The operator's own lines ("mine") are never changed by the
# tool; the auto lines are the tool's proposals, replaced only on regeneration. Stored as one JSON
# object on brands.content_context:
#   {"mine": [{text, kind, sources}], "auto": [{text, kind, sources}],
#    "dismissed": [normalized text], "auto_generated_at": iso}
# kind ∈ {"source", "writing"} once a line has been read ("" = not read yet); sources =
# [{name, domains: [bare domain]}] for a source line.
# ─────────────────────────────────────────────────────────────────────────────────────────────
CI_MAX_MINE = 20
CI_MAX_AUTO = 8
CI_MAX_SOURCE_ORGS = 3
_CI_DOMAIN_RE = re.compile(
    r"(?<![\w@.-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:com|org|gov|net|edu|int|io|co|ai|us|"
    r"uk|ca|au|nz|in|de|fr|eu|info|health|care|mil)(?:\.[a-z]{2})?)(?![\w-])", re.IGNORECASE)


def ci_norm(text):
    """Normalized form of an instruction line, for de-duplication and the dismissed list."""
    t = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return t.rstrip(" .;:!")


def ci_norm_domain(d):
    d = re.sub(r"^https?://", "", str(d or "").strip().lower())
    d = d.split("/")[0].split("?")[0].strip().strip(".")
    if d.startswith("www."):
        d = d[4:]
    return d if ("." in d and re.fullmatch(r"[a-z0-9.-]+", d)) else ""


def _ci_item(raw):
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, dict):
        return None
    text = re.sub(r"\s+", " ", str(raw.get("text") or "")).strip()
    if not text:
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    kind = kind if kind in ("source", "writing") else ""
    sources = []
    if kind == "source":
        for s in (raw.get("sources") or []):
            if not isinstance(s, dict):
                continue
            doms = []
            for d in (s.get("domains") or []):
                nd = ci_norm_domain(d)
                if nd and nd not in doms:
                    doms.append(nd)
            name = str(s.get("name") or "").strip() or (doms[0] if doms else "")
            if name:
                sources.append({"name": name[:80], "domains": doms[:4]})
    out = {"text": text[:300], "kind": kind}
    if kind == "source":
        out["sources"] = sources[:CI_MAX_SOURCE_ORGS]
    return out


def ci_load(raw):
    """Parse a stored content_context (JSON string / dict / None) into the normalized shape."""
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw) if raw.strip() else {}
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    ctx = {"mine": [], "auto": [], "dismissed": [], "auto_generated_at": ""}
    seen = set()
    for r in (data.get("mine") or []):
        it = _ci_item(r)
        if it and ci_norm(it["text"]) not in seen:
            seen.add(ci_norm(it["text"]))
            ctx["mine"].append(it)
    ctx["mine"] = ctx["mine"][:CI_MAX_MINE]
    for r in (data.get("auto") or []):
        it = _ci_item(r)
        if it and ci_norm(it["text"]) not in seen:
            seen.add(ci_norm(it["text"]))
            ctx["auto"].append(it)
    ctx["auto"] = ctx["auto"][:CI_MAX_AUTO]
    ctx["dismissed"] = sorted({ci_norm(x) for x in (data.get("dismissed") or []) if ci_norm(x)})
    ctx["auto_generated_at"] = str(data.get("auto_generated_at") or "")
    return ctx


def ci_merged(ctx):
    """The instructions generation follows: every line of yours first, then the auto lines that are
    neither a duplicate of yours nor dismissed. Each item carries origin 'yours' / 'auto'."""
    ctx = ctx if isinstance(ctx, dict) and "mine" in ctx else ci_load(ctx)
    out, seen = [], set()
    for it in ctx["mine"][:CI_MAX_MINE]:
        seen.add(ci_norm(it["text"]))
        out.append(dict(it, origin="yours"))
    dismissed = set(ctx.get("dismissed") or [])
    n = 0
    for it in ctx["auto"]:
        k = ci_norm(it["text"])
        if k in seen or k in dismissed or n >= CI_MAX_AUTO:
            continue
        seen.add(k)
        n += 1
        out.append(dict(it, origin="auto"))
    return out


def ci_merge_state(stored, mine_texts=None, auto_items=None, dismissed=None):
    """Apply an edit from the UI to the stored context, keeping every reading whose text is unchanged.
    mine_texts — the textarea lines (None = keep stored). auto_items — the auto lines still shown
    (None = keep stored); a stored auto line missing from it is DISMISSED, so regeneration never brings
    it back. dismissed — extra texts to dismiss."""
    ctx = ci_load(stored)
    pool = {}
    for it in ctx["auto"] + ctx["mine"]:   # mine readings win over auto readings of the same text
        if it.get("kind"):
            pool[ci_norm(it["text"])] = it
    incoming_auto = None
    if auto_items is not None:
        incoming_auto = [x for x in (_ci_item(r) for r in auto_items) if x]
        for it in incoming_auto:
            if it.get("kind") and ci_norm(it["text"]) not in pool:
                pool[ci_norm(it["text"])] = it
    if mine_texts is not None:
        new_mine, seen = [], set()
        for t in mine_texts:
            t = re.sub(r"\s+", " ", str(t or "")).strip()
            k = ci_norm(t)
            if not t or k in seen:
                continue
            seen.add(k)
            new_mine.append(dict(pool[k], text=t) if k in pool else {"text": t, "kind": ""})
        ctx["mine"] = [x for x in (_ci_item(m) for m in new_mine) if x][:CI_MAX_MINE]
    mine_keys = {ci_norm(m["text"]) for m in ctx["mine"]}
    dis = set(ctx["dismissed"])
    if incoming_auto is not None:
        kept = {ci_norm(a["text"]) for a in incoming_auto}
        for a in ctx["auto"]:
            k = ci_norm(a["text"])
            if k not in kept and k not in mine_keys:
                dis.add(k)
        ctx["auto"] = [pool.get(ci_norm(a["text"]), a) for a in incoming_auto]
    ctx["auto"] = [a for a in ctx["auto"] if ci_norm(a["text"]) not in mine_keys][:CI_MAX_AUTO]
    for d in (dismissed or []):
        if ci_norm(d):
            dis.add(ci_norm(d))
    ctx["dismissed"] = sorted(dis - mine_keys)
    return ctx


_CI_SOURCE_WORDS_RE = re.compile(r"\b(source|sources|sourced|sourcing|cite|cites|citing|citation|citations|"
                                 r"reference|references|link to|according to)\b", re.IGNORECASE)


def _ci_read_typed_domains(text):
    """A line whose author typed the site(s) is read without the model: its domains are taken as written."""
    doms = []
    for m in _CI_DOMAIN_RE.finditer(text or ""):
        d = ci_norm_domain(m.group(1))
        if d and d not in doms:
            doms.append(d)
    if not doms:
        return None
    name = re.sub(r"https?://\S+", " ", text or "")
    name = _CI_DOMAIN_RE.sub(" ", name)
    name = re.sub(r"(?i)\b(should|must|always|please|only|for|the|a|an|on|in|about|to|and|or|pages?|"
                  r"site|website|their|its|official)\b|\b(source|sources|sourced|cite|citations?|"
                  r"reference|references|use|from|link to|according to)\b", " ", name)
    name = re.sub(r"[()\[\],;:]+", " ", name)
    # keep only the capitalized words (an organisation's name or acronym: "AAP", "Mayo Clinic"),
    # otherwise the domain itself names the source
    caps = [w for w in re.sub(r"\s+", " ", name).strip(" -–—.").split() if w[:1].isupper()]
    name = " ".join(caps) if 0 < len(caps) <= 5 else doms[0]
    return {"kind": "source", "sources": [{"name": name[:80], "domains": doms[:4]}]}


def build_content_context(claude, brand, stored=None, mine_texts=None, regenerate_auto=False,
                          vertical=None):
    """Read the operator's unread lines and (when asked, or never done) propose the AUTO lines.

    ONE model call covers both. A line with a typed domain is read without the model. A source whose
    domains come back empty falls back to web-search domain resolution (at most 3 lookups). Never
    raises: on any failure the stored context comes back unchanged (with `mine_texts` applied) and
    `auto_generated_at` is NOT stamped, so the next generation retries."""
    b = brand or {}
    ctx = ci_load(stored if stored is not None else b.get("content_context"))
    if mine_texts is not None:
        ctx = ci_merge_state(ctx, mine_texts=mine_texts)
    for it in ctx["mine"]:
        if not it.get("kind") and _CI_SOURCE_WORDS_RE.search(it["text"]):
            typed = _ci_read_typed_domains(it["text"])
            if typed:
                it.update(typed)
    unread = [it for it in ctx["mine"] if not it.get("kind")]
    need_auto = bool(regenerate_auto or not ctx.get("auto_generated_at"))
    if not unread and not need_auto:
        return ctx
    if claude is None:
        return ctx

    name = (b.get("name") or "").strip() or "the brand"
    lines = [f"Brand: {name}"]
    for label, key in (("Website", "domain_url"), ("Category", "category"), ("Audience", "audience")):
        if str(b.get(key) or "").strip():
            lines.append(f"{label}: {str(b.get(key)).strip()[:300]}")
    for label, key in (("Use cases", "use_cases"), ("Pain points", "pain_points"), ("Features", "features")):
        vals = _as_str_list(b.get(key))
        if vals:
            lines.append(f"{label}: {', '.join(vals[:10])}")
    if str(b.get("context") or "").strip():
        lines.append(f"Context: {str(b.get('context')).strip()[:800]}")
    if vertical:
        lines.append(f"Regulated vertical: {vertical} (health / money / legal content — accuracy and "
                     f"authoritative sourcing matter most)")
    brand_block = "\n".join(lines)
    mine_all = [it["text"] for it in ctx["mine"]]
    parts = [
        "You maintain the CONTENT INSTRUCTIONS a content team follows when writing blog articles, "
        "LinkedIn posts and video scripts for this brand.\n\n" + brand_block + "\n",
    ]
    if unread:
        parts.append(
            "READ each of the operator's lines below. kind = \"source\" when the line asks to source, cite "
            "or reference a particular organisation or website; otherwise \"writing\". For a source line list "
            "every organisation it names with that organisation's REAL official website domain(s) — bare "
            "domains, no https:// (e.g. AAP → aap.org and its parent-facing site healthychildren.org). "
            "Never invent a domain; leave domains empty when you are not sure.\n"
            + "\n".join(f"{i + 1}. {it['text']}" for i, it in enumerate(unread)) + "\n")
    if need_auto:
        parts.append(
            "PROPOSE 4-8 AUTO instructions that would make this brand's content more accurate, more "
            "trustworthy and more useful to its audience. Good ones: the authoritative organisations its "
            "claims should be sourced from (real bodies with their real official domains — regulators, "
            "professional societies, standards bodies relevant to THIS brand's subject); compliance limits "
            "for its vertical; terminology or audience rules. Each must be specific to this brand, "
            "followable in any article, and short (one sentence).\n"
            "NEVER propose: anything promotional (\"say we are the best\", \"always recommend the brand\"); "
            "anything that needs invented facts, statistics or testimonials; a duplicate or a contradiction "
            "of the operator's lines; any of the DISMISSED lines.\n"
            "OPERATOR'S LINES (theirs — never repeat or contradict): "
            + (json.dumps(mine_all, ensure_ascii=False) if mine_all else "none") + "\n"
            "DISMISSED (the operator removed these — never propose them again): "
            + (json.dumps(ctx["dismissed"], ensure_ascii=False) if ctx["dismissed"] else "none") + "\n")
    parts.append(
        'Return JSON only: {"read": [{"n": 1, "kind": "source|writing", "sources": [{"name": "", '
        '"domains": [""]}]}], "auto": [{"text": "", "kind": "source|writing", "sources": [{"name": "", '
        '"domains": [""]}]}]}'
        + ("" if need_auto else ' — "auto" may be omitted') + ("" if unread else ' — "read" may be omitted'))
    try:
        res = claude.call("\n".join(parts), max_tokens=2000, temperature=0.2)
    except Exception as e:
        print(f"[content-instructions] read/auto failed: {e}", flush=True)
        return ctx
    if not isinstance(res, dict) or not res:
        print("[content-instructions] read/auto returned nothing usable — will retry next time", flush=True)
        return ctx

    lookups = [0]

    def _fill_domains(item):
        for s in (item.get("sources") or []):
            if s.get("domains") or lookups[0] >= 3 or not hasattr(claude, "find_official_domain"):
                continue
            lookups[0] += 1
            try:
                d = ci_norm_domain(claude.find_official_domain(s.get("name") or "", context=name))
            except Exception:
                d = ""
            if d:
                s["domains"] = [d]
        return item

    for r in (res.get("read") or []):
        if not isinstance(r, dict):
            continue
        try:
            idx = int(r.get("n")) - 1
        except Exception:
            continue
        if 0 <= idx < len(unread):
            got = _ci_item(dict(r, text=unread[idx]["text"]))
            if got and got.get("kind"):
                unread[idx].update(_fill_domains(got))
    if need_auto and isinstance(res.get("auto"), list):
        mine_keys = {ci_norm(t) for t in mine_all}
        dismissed = set(ctx["dismissed"])
        auto, seen = [], set()
        for r in res["auto"]:
            it = _ci_item(r)
            if not it or not it.get("kind"):
                continue
            k = ci_norm(it["text"])
            if k in mine_keys or k in dismissed or k in seen:
                continue
            seen.add(k)
            auto.append(_fill_domains(it))
            if len(auto) >= CI_MAX_AUTO:
                break
        ctx["auto"] = auto
        ctx["auto_generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        print(f"[content-instructions] {len(auto)} auto instruction(s) generated for {name}", flush=True)
    return ctx


def _as_str_list(raw):
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str) and raw.strip():
        try:
            v = json.loads(raw)
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
        except Exception:
            pass
        return [s.strip() for s in raw.split(",") if s.strip()]
    return []
