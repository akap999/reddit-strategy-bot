# Reddit Strategy Bot

Interactive CLI tool for managing subreddit content strategy — generates subreddits, posts, and comment trees with anti-detection measures, all persisted in a local SQLite database.

## Setup

**Prerequisites:** Python 3.8+

```bash
# Install dependencies
pip3 install -r requirements.txt

# Set your Anthropic API key

```

## How to Run

```bash
# Web dashboard (recommended)
python3 app.py
# Open http://localhost:5000 in your browser

# Interactive CLI mode
python3 bot.py

# Legacy CSV mode (original comment_generator.py)
python3 bot.py --legacy posts.csv --domain example.com
```

On first run, the bot creates `strategy_bot.db` in the project directory and prompts for your API key if not set. Both the web dashboard and CLI share the same database.

## Workflow

```
1. Create Subreddit    Input brand names -> get name suggestions (with Reddit availability check) -> generate rules/description
2. Add Brands          Add one or more brands to a subreddit (auto-extract info from domain or enter manually)
3. Generate Fillers    Create organic community posts with NO brand angle (seeds the subreddit)
4. Generate Posts      Create brand-related posts (posts NEVER mention the brand — they're generic domain questions)
5. Generate Comments   Create comment trees for posts (30% mention the brand, spread across days)
6. Export Schedule     Export day-by-day posting schedule to CSV for manual posting
```

## Web Dashboard

The web dashboard at `http://localhost:5000` provides a visual interface for all bot operations:

- **Dashboard** — Overview stats (subreddits, brands, posts, comments)
- **Subreddits** — Create subreddits with AI-generated names (availability check), manage brands
- **Posts** — Generate brand posts and filler posts, filter by subreddit/status, publish with Reddit URL
- **Comments** — Generate comment trees, view nested comment threads, manage individual comments
- **Analytics** — Subreddit overview, brand performance (mention ratio gauge), content health checks, posting schedule timeline
- **Export** — Download CSV exports for all data or posting schedule

All generation operations run in background threads with progress indicators. Long-running tasks (10-60s) poll for completion automatically.

## CLI Menu Structure

```
=== Reddit Strategy Bot ===
[1] Manage Subreddits      Create subreddits, add brands, link Reddit URLs, mark posts as published
[2] Manage Posts            Generate brand posts, filler posts, custom posts, view/delete/regenerate
[3] Manage Comments         Generate comment trees (fresh or live posts), view trees, delete/regenerate
[4] Analytics Dashboard     Subreddit overview, brand performance, content health, schedule status
[5] Export                  Export all data or posting schedule to CSV
[q] Quit
```

## Key Concepts

**Posts never mention brands.** Posts are generic domain questions/experiences (e.g., "Best online TRT clinics with good lab monitoring?"). Brands only appear in comments at a 30% mention rate. This mirrors how real Reddit works.

**Day-based scheduling.** Posts and comments are assigned `suggested_post_day` values to spread content naturally. Filler posts go first (days 0-3), brand posts follow, comments are spread 3-7 days after their post, and brand-mentioning comments never appear on day 1.

**Filler posts.** Organic community posts with no brand angle, used to make a new subreddit look real before brand-adjacent content appears.

**20 personas + 12 structures.** Each comment is assigned a unique persona (skeptic, newbie, veteran, data_nerd, etc.) and structure template (story arc, direct answer, comparison, etc.) to prevent pattern detection.

**Cross-subreddit dedup.** When generating content for a brand, the bot checks history across ALL subreddits where that brand exists to prevent duplicate content.

**Comment trees.** For fresh posts, the bot generates full conversation trees: ~60% top-level comments + ~40% replies, with different personas for each.

## Project Structure

```
subreddits/
  app.py                     Web dashboard — Flask server + API routes
  bot.py                     Entry point — interactive CLI
  config.py                  API config, constants, scheduling defaults
  db.py                      SQLite persistence (5 tables, full CRUD + analytics queries)
  analytics.py               Analytics queries + ASCII dashboard rendering
  requirements.txt           Python dependencies
  templates/
    index.html               Single-page web dashboard (Tailwind CSS, vanilla JS)
  generators/
    base.py                  20 personas, 12 structures, banned phrases, ClaudeClient
    subreddit_gen.py         Name generation + Reddit availability check
    post_gen.py              Post generation, AI-query scoring, filler posts, image prompts
    comment_gen.py           Comment trees, 30% brand ratio, live post support
  cli/
    menus.py                 Input helpers, display functions
    subreddit_flow.py        Create subreddit, add brands, link URLs
    post_flow.py             Generate/view/delete posts
    comment_flow.py          Generate/view/delete comment trees
    analytics_flow.py        4-view analytics dashboard
  comment_generator.py       Original legacy script (untouched, backward compatible)
```

## Database

SQLite database at `strategy_bot.db` with 5 tables:

| Table | Purpose |
|-------|---------|
| `subreddits` | Subreddit name, domain, description, rules, sidebar |
| `brands` | Brand name, context, keywords, linked to a subreddit |
| `post_urls` | Links live Reddit URLs to subreddits/posts |
| `posts` | Generated posts with storyline, AI-query score, scheduling, status |
| `comments` | Generated comments with persona, structure, brand mention flag, tree structure |

**Access the database:**
- Through the bot: `[4] Analytics Dashboard`
- Direct SQL: `sqlite3 strategy_bot.db`
- Export: `[5] Export` menu in the bot

## Legacy Mode

The original `comment_generator.py` is preserved and works exactly as before:

```bash
# Via bot.py wrapper
python3 bot.py --legacy posts.csv --domain example.com

# Or directly
python3 comment_generator.py posts.csv --domain example.com --brand "BrandName" --context "What brand does"
```

See `python3 comment_generator.py --help` for all options.

## Dependencies

| Package | Purpose |
|---------|---------|
| `anthropic` | Claude API client for all AI generation |
| `requests` | Reddit API calls, brand info extraction, image search |
| `beautifulsoup4` | HTML parsing for brand info extraction from domains |
| `flask` | Web dashboard server |

## Cost Estimate

Approximate API costs per operation (using Claude Sonnet):

| Operation | API Calls | Est. Cost |
|-----------|-----------|-----------|
| 1 post generation | ~3 | ~$0.02 |
| 10 comments (fresh post tree) | ~6 | ~$0.05 |
| 5 posts + 10 comments each | ~33 | ~$0.35-0.50 |
