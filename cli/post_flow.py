"""Interactive flows for post generation."""

import json
from cli.menus import (
    print_header, print_menu, get_choice, get_input, get_number,
    confirm, select_from_list, display_post
)
from generators.post_gen import PostGenerator
from db import Database


def post_menu(db: Database, post_gen: PostGenerator):
    """Main post management menu."""
    while True:
        print_menu("Manage Posts", [
            ("1", "Generate Brand Posts"),
            ("2", "Generate Filler Posts"),
            ("3", "Add Custom Post"),
            ("4", "View Posts"),
            ("5", "Delete / Regenerate Post"),
        ])
        choice = get_choice(valid=["1", "2", "3", "4", "5", "b"])

        if choice == "b":
            return
        elif choice == "1":
            generate_brand_posts_flow(db, post_gen)
        elif choice == "2":
            generate_filler_flow(db, post_gen)
        elif choice == "3":
            add_custom_post_flow(db)
        elif choice == "4":
            view_posts_flow(db)
        elif choice == "5":
            delete_regenerate_post_flow(db, post_gen)


def generate_brand_posts_flow(db: Database, post_gen: PostGenerator):
    """Generate posts for a brand (posts NEVER mention the brand)."""
    print_header("Generate Brand Posts")

    # Select subreddit
    subreddits = db.list_subreddits()
    sub = select_from_list(subreddits, lambda s: f"r/{s['name']} ({s['brand_count']} brands)", "Select subreddit")
    if not sub:
        return

    # Select brand
    brands = db.list_brands(sub["id"])
    if not brands:
        print("  No brands in this subreddit. Add one first.")
        return
    brand = select_from_list(brands, lambda b: f"{b['name']} — {b['context'][:40]}", "Select brand")
    if not brand:
        return

    count = get_number("How many posts?", default=5, min_val=1, max_val=20)

    # Custom topics
    custom_topics = None
    if confirm("Add custom post topics?"):
        custom_topics = []
        while True:
            topic = get_input("Post title/topic (or 'done')", required=False)
            if not topic or topic.lower() == "done":
                break
            custom_topics.append(topic)

    # Estimate cost
    api_calls = count * 3  # generate + score + image prompt
    print(f"\n  Estimated API calls: ~{api_calls}")
    if not confirm("Proceed?", default=True):
        return

    print(f"\n  Generating {count} posts for {brand['name']} in r/{sub['name']}...")
    print(f"  (Posts will NOT mention {brand['name']} — they are generic domain posts)")

    posts = post_gen.generate_posts(sub, brand, count, custom_topics)

    if not posts:
        print("  Generation failed.")
        return

    # Display results
    print(f"\n  Generated {len(posts)} posts:")
    for i, post in enumerate(posts, 1):
        display_post(post, i)

    print(f"\n  All posts saved to database.")


def generate_filler_flow(db: Database, post_gen: PostGenerator):
    """Generate organic filler posts with no brand angle."""
    print_header("Generate Filler Posts")

    subreddits = db.list_subreddits()
    sub = select_from_list(subreddits, lambda s: f"r/{s['name']}", "Select subreddit")
    if not sub:
        return

    count = get_number("How many filler posts?", default=3, min_val=1, max_val=10)

    print(f"\n  Generating {count} organic filler posts for r/{sub['name']}...")
    posts = post_gen.generate_filler_posts(sub, count)

    if not posts:
        print("  Generation failed.")
        return

    print(f"\n  Generated {len(posts)} filler posts:")
    for i, post in enumerate(posts, 1):
        display_post(post, i)

    print(f"\n  All filler posts saved.")


def add_custom_post_flow(db: Database):
    """Manually enter a custom post."""
    subreddits = db.list_subreddits()
    sub = select_from_list(subreddits, lambda s: f"r/{s['name']}", "Select subreddit")
    if not sub:
        return

    # Optionally link to a brand
    brands = db.list_brands(sub["id"])
    brand_id = None
    if brands:
        print("\n  Link to a brand? (optional)")
        brand = select_from_list(brands, lambda b: b["name"], "Select brand (or back for no brand)")
        if brand:
            brand_id = brand["id"]

    title = get_input("Post title")
    print("  Post body (enter text, then press Enter twice to finish):")
    body_lines = []
    empty_count = 0
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            break
        if line == "":
            empty_count += 1
            if empty_count >= 2:
                break
            body_lines.append("")
        else:
            empty_count = 0
            body_lines.append(line)
    body = "\n".join(body_lines).strip()

    storyline = get_input("Storyline type (experience/question/complaint/comparison/discovery/psa)", default="question")

    post_id = db.save_post(
        subreddit_id=sub["id"],
        brand_id=brand_id,
        title=title,
        body=body,
        storyline=storyline,
        is_custom=1,
        status="complete",
    )
    print(f"\n  Custom post saved (ID: {post_id})")


def view_posts_flow(db: Database):
    """View posts for a subreddit."""
    subreddits = db.list_subreddits()
    sub = select_from_list(subreddits, lambda s: f"r/{s['name']} ({s['post_count']} posts)", "Select subreddit")
    if not sub:
        return

    # Filter options
    print_menu("Filter", [("1", "All posts"), ("2", "Brand posts only"), ("3", "Filler posts only")])
    filter_choice = get_choice(valid=["1", "2", "3", "b"])
    if filter_choice == "b":
        return

    posts = db.get_posts(sub["id"], include_filler=True)

    if filter_choice == "2":
        posts = [p for p in posts if not p.get("is_filler")]
    elif filter_choice == "3":
        posts = [p for p in posts if p.get("is_filler")]

    if not posts:
        print("  No posts found.")
        return

    print(f"\n  Posts in r/{sub['name']} ({len(posts)} total):")
    for i, post in enumerate(posts, 1):
        display_post(post, i)

    # Option to view full post
    if confirm("View a post in detail?"):
        post = select_from_list(posts, lambda p: f"\"{p['title'][:50]}\"", "Select post")
        if post:
            print(f"\n  Title: {post['title']}")
            print(f"  Body: {post['body'][:500]}")
            print(f"  Storyline: {post['storyline']} | AI-query: {post['ai_query_score']}/10")
            print(f"  Status: {post['status']} | Day: {post['suggested_post_day']}")
            if post.get("image_prompt"):
                print(f"  Image search: {post['image_prompt']}")
            url = db.get_url_for_post(post["id"])
            if url:
                print(f"  Reddit URL: {url}")


def delete_regenerate_post_flow(db: Database, post_gen: PostGenerator):
    """Delete or regenerate a specific post."""
    subreddits = db.list_subreddits()
    sub = select_from_list(subreddits, lambda s: f"r/{s['name']}", "Select subreddit")
    if not sub:
        return

    posts = db.get_posts(sub["id"])
    if not posts:
        print("  No posts.")
        return

    post = select_from_list(posts, lambda p: f"[{p['status']}] \"{p['title'][:50]}\"", "Select post")
    if not post:
        return

    print_menu("Action", [("1", "Delete this post"), ("2", "Regenerate this post")], back=True)
    action = get_choice(valid=["1", "2", "b"])

    if action == "1":
        if confirm(f"Delete \"{post['title'][:40]}\" and all its comments?"):
            db.delete_post(post["id"])
            print("  Post deleted.")
    elif action == "2":
        brand = db.get_brand(post["brand_id"]) if post.get("brand_id") else None
        if not brand:
            print("  Cannot regenerate: no brand linked.")
            return
        print("  Deleting old post and generating replacement...")
        storyline = post["storyline"]
        db.delete_post(post["id"])
        new_posts = post_gen.generate_posts(sub, brand, 1)
        if new_posts:
            print(f"  New post: \"{new_posts[0]['title'][:50]}\"")
        else:
            print("  Regeneration failed.")
