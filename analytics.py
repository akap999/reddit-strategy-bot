"""Analytics queries and terminal dashboard rendering."""

import json
from db import Database


def render_bar(value, max_value, width=20):
    """Render an ASCII bar chart segment."""
    if max_value == 0:
        filled = 0
    else:
        filled = int(value / max_value * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


def subreddit_overview(db: Database, subreddit_id):
    """Render subreddit overview stats."""
    sub = db.get_subreddit(subreddit_id)
    if not sub:
        print("  Subreddit not found.")
        return

    stats = db.get_stats_for_subreddit(subreddit_id)
    brands = db.list_brands(subreddit_id)
    brand_names = [b["name"] for b in brands]

    posts = stats["posts"]
    comments = stats["comments"]

    total_comments = comments["total_comments"] or 0
    brand_mentions = comments["with_brand"] or 0
    mention_pct = int(brand_mentions / total_comments * 100) if total_comments > 0 else 0
    avg_score = comments["avg_validation_score"]
    avg_score_str = f"{avg_score:.1f}" if avg_score else "N/A"

    print(f"\n  r/{sub['name']}")
    print(f"  {'='*50}")
    print(f"  Brands: {len(brands)} ({', '.join(brand_names[:5])})")
    print(f"  Posts:  {posts['total_posts']} total ({posts['brand_posts']} brand, {posts['filler_posts']} filler)")
    print(f"          {posts['published_posts']} published, {posts['complete_posts']} ready, {posts['draft_posts']} draft")
    print(f"  Comments: {total_comments} total | {brand_mentions} with brand mention ({mention_pct}%)")
    print(f"  Avg validation score: {avg_score_str}")

    # Storyline distribution
    dist = db.get_storyline_distribution(subreddit_id)
    if dist:
        max_count = max(dist.values()) if dist else 1
        print(f"\n  Storyline Distribution:")
        all_types = ["question", "experience", "comparison", "complaint", "discovery", "psa"]
        for st in all_types:
            count = dist.get(st, 0)
            bar = render_bar(count, max_count, 20)
            label = f"  <- underrepresented" if count == 0 and max_count > 0 else ""
            print(f"    {st:12s} {bar} {count}{label}")


def brand_performance(db: Database, brand_name=None):
    """Render brand performance stats across all subreddits."""
    if not brand_name:
        # Show all brands
        all_subs = db.list_subreddits()
        seen_brands = set()
        for sub in all_subs:
            brands = db.list_brands(sub["id"])
            for b in brands:
                if b["name"] not in seen_brands:
                    seen_brands.add(b["name"])
                    _render_brand_stats(db, b["name"])
    else:
        _render_brand_stats(db, brand_name)


def _render_brand_stats(db: Database, brand_name):
    """Render stats for a single brand."""
    mention_data = db.get_brand_mention_ratio(brand_name=brand_name)
    personas = db.get_persona_distribution(brand_name=brand_name)
    total_posts = len(db.get_all_post_titles_for_brand(brand_name))
    total_comments = mention_data["total"]
    with_brand = mention_data["with_brand"]
    ratio = mention_data["ratio"]

    print(f"\n  {brand_name}")
    print(f"  {'='*40}")
    print(f"  Total posts (across all subreddits): {total_posts}")
    print(f"  Comments mentioning brand: {with_brand} / {total_comments} ({int(ratio*100)}%)")

    target = 30
    if total_comments > 0:
        if abs(ratio * 100 - target) <= 5:
            status = "on target"
        elif ratio * 100 < target:
            status = "below target"
        else:
            status = "above target"
        print(f"    Target: {target}% | Status: {status}")

    if personas:
        unique_personas = len(personas)
        print(f"  Unique personas used: {unique_personas} / 20")
        top_3 = sorted(personas.items(), key=lambda x: x[1], reverse=True)[:3]
        print(f"    Most used: {', '.join(f'{p}({c})' for p, c in top_3)}")


def content_health(db: Database, subreddit_id):
    """Check for pattern repetition and quality issues."""
    sub = db.get_subreddit(subreddit_id)
    if not sub:
        return

    print(f"\n  Content Health — r/{sub['name']}")
    print(f"  {'='*50}")

    # Get all comments for this subreddit
    all_posts = db.get_posts(subreddit_id)
    all_comments = []
    for p in all_posts:
        comments = db.get_comments(p["id"])
        all_comments.extend(comments)

    if not all_comments:
        print("  No comments to analyze.")
        return

    # Check duplicate openings
    openings = []
    for c in all_comments:
        words = c["body"].split()[:5]
        openings.append(" ".join(words).lower())

    from collections import Counter
    opening_counts = Counter(openings)
    dupes = [(o, cnt) for o, cnt in opening_counts.items() if cnt > 1]

    if dupes:
        print(f"  Duplicate opening phrases: {len(dupes)}")
        for o, cnt in dupes[:3]:
            print(f"    \"{o}...\" used {cnt}x")
    else:
        print(f"  Duplicate opening phrases: 0")

    # Check consecutive persona usage
    personas_seq = [c.get("persona_id", "") for c in all_comments if c.get("persona_id")]
    max_consecutive = 1
    current_run = 1
    for i in range(1, len(personas_seq)):
        if personas_seq[i] == personas_seq[i-1]:
            current_run += 1
            max_consecutive = max(max_consecutive, current_run)
        else:
            current_run = 1

    consecutive_ok = max_consecutive <= 3
    print(f"  Same persona used >3x consecutively: {'NO' if consecutive_ok else 'YES'} (max run: {max_consecutive})")

    # Check low quality comments
    low_quality = [c for c in all_comments if c.get("validation_score") and c["validation_score"] < 6]
    if low_quality:
        print(f"  Comments below validation threshold (< 6): {len(low_quality)}")
        for c in low_quality[:3]:
            print(f"    - Comment #{c['id']} (score {c['validation_score']}): \"{c['body'][:50]}...\"")
    else:
        print(f"  Comments below validation threshold: 0")

    # Average comment length
    lengths = [len(c["body"].split()) for c in all_comments]
    avg_len = sum(lengths) / len(lengths) if lengths else 0
    print(f"\n  Avg comment length: {int(avg_len)} words (target: 40-150)")

    # Brand mention placement
    brand_comments = [c for c in all_comments if c.get("mentions_brand")]
    if brand_comments:
        # Analyze where brand appears in comment (early/mid/late)
        placements = {"early": 0, "mid": 0, "late": 0}
        for c in brand_comments:
            body = c["body"].lower()
            total_len = len(body)
            # Find brand position (search for any brand in the subreddit)
            brands = db.list_brands(subreddit_id)
            for b in brands:
                pos = body.find(b["name"].lower())
                if pos >= 0:
                    relative = pos / total_len if total_len > 0 else 0.5
                    if relative < 0.33:
                        placements["early"] += 1
                    elif relative < 0.66:
                        placements["mid"] += 1
                    else:
                        placements["late"] += 1
                    break

        total_placed = sum(placements.values())
        if total_placed > 0:
            print(f"  Brand mention placement:")
            print(f"    Early (first third): {placements['early']} ({int(placements['early']/total_placed*100)}%)")
            print(f"    Mid (middle third): {placements['mid']} ({int(placements['mid']/total_placed*100)}%)")
            print(f"    Late (last third): {placements['late']} ({int(placements['late']/total_placed*100)}%)")


def posting_schedule_status(db: Database, subreddit_id):
    """Show day-by-day posting schedule with status."""
    sub = db.get_subreddit(subreddit_id)
    if not sub:
        return

    schedule = db.get_schedule_status(subreddit_id)
    if not schedule:
        print("  No scheduled content.")
        return

    print(f"\n  Posting Schedule — r/{sub['name']}")
    print(f"  {'='*50}")

    for day in sorted(schedule.keys()):
        entries = schedule[day]
        posts = entries["posts"]
        comments = entries["comments"]

        print(f"\n  Day {day}:")
        for p in posts:
            status = {"draft": "DRAFT", "complete": "READY", "published": "PUBLISHED"}.get(p["status"], p["status"])
            filler = "Filler" if p.get("is_filler") else "Brand post"
            url = p.get("reddit_url", "")
            url_text = f" -> {url[:40]}" if url else ""
            print(f"    [{status:9s}] {filler}: \"{p['title'][:45]}\"{url_text}")

        if comments:
            brand_comments = sum(1 for c in comments if c.get("mentions_brand"))
            print(f"    [{len(comments)} comments scheduled ({brand_comments} with brand mention)]")
