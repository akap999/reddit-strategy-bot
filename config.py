"""Configuration constants and environment loading for Reddit Strategy Bot."""

import os

# Load .env file if present
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                k = key.strip()
                if value and not os.environ.get(k):
                    os.environ[k] = value

# --- API Configuration ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Overridable via the CLAUDE_MODEL env var so a model retirement is a config change, not a
# redeploy. Default = Sonnet 4.6 (successor to the retired claude-sonnet-4-20250514).
DEFAULT_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# --- Authentication ---
SECRET_KEY = os.environ.get("SECRET_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
ALLOWED_EMAILS = [e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()]

# --- Database ---
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_bot.db"))

# --- Prompt Versioning ---
PROMPT_VERSION = "v1.0"

# --- AI-Search embedding relevance gate (optional / graceful) ---
# Drops generated posts whose (title+body) is semantically too far from their target
# fan-out query. OFF unless OPENAI_API_KEY is set; threshold tuned so natural Reddit
# titles pass and only genuine drift fails.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
try:
    EMBED_THRESHOLD = float(os.environ.get("EMBED_THRESHOLD", "0.68"))
except (TypeError, ValueError):
    EMBED_THRESHOLD = 0.68

# --- Storyline Types ---
STORYLINE_TYPES = {
    "experience": "Personal experience with a product/service in this space",
    "question": "Genuine question seeking advice or recommendations",
    "complaint": "Frustration with a problem the brand's domain addresses",
    "comparison": "Comparing options, asking which is better",
    "discovery": "Just found out about something, sharing initial impressions",
    "psa": "Public service announcement or tip for the community",
}

# --- GEO Post Generation: Intent-Balanced Batches ---
# Only these batch sizes are allowed; each group of 3 gets exactly 1 of each intent.
POST_BATCH_SIZES = (3, 6, 9)
INTENT_TYPES = ("commercial", "comparison", "informational")

# --- AI Query Patterns (high-value for AI model answers) ---
AI_QUERY_PATTERNS = [
    "best {category} for {use_case}",
    "which {category} should I use",
    "{category} recommendations",
    "top {category} compared",
    "{category} vs {category}",
    "is {specific_thing} worth it",
    "has anyone tried {category}",
    "looking for {category} advice",
    "what do you use for {use_case}",
]

# --- Scheduling Defaults ---
DEFAULT_BRAND_MENTION_RATIO = 0.3
POST_SPREAD_FACTOR = 1.5  # posts spread across count * factor days
COMMENT_SPREAD_DAYS = 5   # comments for a post spread across this many days
FILLER_LEAD_DAYS = 3      # filler posts lead brand posts by this many days

# --- Reddit ---
REDDIT_USER_AGENT = "SubredditStrategyBot/2.0 (by /u/strategy_bot_admin)"
REDDIT_PROXY_URL = os.environ.get("REDDIT_PROXY_URL", "")  # Cloudflare Worker URL for Reddit proxy
# Optional: a distinct-IP residential proxy (IPRoyal) used as a FALLBACK egress when the normal Reddit
# path (Cloudflare worker / cloud IP) is blocked or throttled. Gateway URL, e.g.
# http://USER:PASS@geo.iproyal.com:12321 . Residential bandwidth is METERED, so the app only routes
# Reddit through it after a block (see search/reddit_bot.py) to conserve the plan's GB.
REDDIT_HTTP_PROXY = os.environ.get("REDDIT_HTTP_PROXY", "")
# Optional: Brave Search API key. When set, Live Search adds a `site:reddit.com`
# discovery fallback leg (independent of Reddit's rate limits). No-op when unset.
# Free key: https://brave.com/search/api
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
