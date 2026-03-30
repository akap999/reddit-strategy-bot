"""Interactive flows for comment generation."""

import json
from cli.menus import (
    print_header, print_menu, get_choice, get_input, get_number,
    confirm, select_from_list, display_comment_tree
)
from generators.comment_gen import CommentGenerator
from db import Database
from config import DEFAULT_BRAND_MENTION_RATIO


def comment_menu(db: Database, comment_gen: CommentGenerator):
    """Main comment management menu."""
    while True:
        print_menu("Manage Comments", [
            ("1", "Generate for Fresh Post (comment tree)"),
            ("2", "Generate for Live Post (Reddit URL)"),
            ("3", "View Comment Trees"),
            ("4", "Delete / Regenerate Comment"),
        ])
        choice = get_choice(valid=["1", "2", "3", "4", "b"])

        if choice == "b":
            return
        elif choice == "1":
            generate_fresh_comments_flow(db, comment_gen)
        elif choice == "2":
            generate_live_comments_flow(db, comment_gen)
        elif choice == "3":
            view_comments_flow(db)
        elif choice == "4":
            delete_regenerate_comment_flow(db, comment_gen)


def generate_fresh_comments_flow(db: Database, comment_gen: CommentGenerator):
    """Generate a full comment tree for a post from our DB (no existing Reddit comments)."""
    print_header("Generate Comments for Fresh Post")

    # Select subreddit
    subreddits = db.list_subreddits()
    sub = select_from_list(subreddits, lambda s: f"r/{s['name']} ({s['post_count']} posts)", "Select subreddit")
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

    # Select post
    posts = db.get_posts(sub["id"])
    if not posts:
        print("  No posts in this subreddit. Generate some first.")
        return
    post = select_from_list(
        posts,
        lambda p: f"[{p['status']}] \"{p['title'][:50]}\" (Day {p['suggested_post_day']})",
        "Select post"
    )
    if not post:
        return

    # Check if post already has comments
    existing_comments = db.get_comments(post["id"])
    if existing_comments:
        print(f"  This post already has {len(existing_comments)} comments.")
        if not confirm("Generate more comments?"):
            return

    num_comments = get_number("How many total comments (top-level + replies)?", default=8, min_val=2, max_val=30)
    brand_ratio = get_number("Brand mention percentage", default=30, min_val=10, max_val=50) / 100

    # Estimate cost
    api_calls = num_comments + 3  # generation + tone + validation
    print(f"\n  Plan: ~{int(num_comments * 0.6)} top-level + ~{int(num_comments * 0.4)} replies")
    print(f"  Brand mentions: ~{int(num_comments * brand_ratio)} of {num_comments} ({int(brand_ratio*100)}%)")
    print(f"  Estimated API calls: ~{api_calls}")

    if not confirm("Proceed?", default=True):
        return

    print(f"\n  Generating comment tree for \"{post['title'][:50]}\"...")
    saved = comment_gen.generate_comment_tree(
        post=post,
        brand=brand,
        num_comments=num_comments,
        brand_mention_ratio=brand_ratio,
        post_day_offset=post.get("suggested_post_day", 0),
    )

    if not saved:
        print("  Generation failed.")
        return

    # Display results
    brand_count = sum(1 for c in saved if c.get("mentions_brand"))
    top_count = sum(1 for c in saved if not c.get("is_reply"))
    reply_count = sum(1 for c in saved if c.get("is_reply"))

    print(f"\n  Generated {len(saved)} comments:")
    print(f"    Top-level: {top_count} | Replies: {reply_count}")
    print(f"    Brand mentions: {brand_count}/{len(saved)} ({int(brand_count/len(saved)*100) if saved else 0}%)")

    # Show the tree
    tree = db.get_comment_tree(post["id"])
    print(f"\n  Comment Tree:")
    display_comment_tree(tree)

    print(f"\n  All comments saved to database.")


def generate_live_comments_flow(db: Database, comment_gen: CommentGenerator):
    """Generate comments for a live Reddit post (fetches existing comments first)."""
    print_header("Generate Comments for Live Post")

    # Select subreddit
    subreddits = db.list_subreddits()
    sub = select_from_list(subreddits, lambda s: f"r/{s['name']}", "Select subreddit")
    if not sub:
        return

    # Select brand
    brands = db.list_brands(sub["id"])
    if not brands:
        print("  No brands in this subreddit. Add one first.")
        return
    brand = select_from_list(brands, lambda b: f"{b['name']}", "Select brand")
    if not brand:
        return

    # Get URL — from linked URLs or manual entry
    url = None
    linked_urls = db.get_post_urls(sub["id"])
    if linked_urls:
        print("\n  Linked Reddit URLs:")
        for i, u in enumerate(linked_urls, 1):
            print(f"    {i}. {u['reddit_url'][:70]}")
        print(f"    {len(linked_urls)+1}. Enter a new URL")
        valid = [str(i) for i in range(1, len(linked_urls) + 2)] + ["b"]
        choice = get_choice("  > ", valid)
        if choice == "b":
            return
        if int(choice) <= len(linked_urls):
            url = linked_urls[int(choice) - 1]["reddit_url"]
        else:
            url = get_input("Reddit post URL")
    else:
        url = get_input("Reddit post URL")

    if not url or "reddit.com" not in url:
        print("  Invalid URL.")
        return

    num_comments = get_number("How many comments to generate?", default=5, min_val=1, max_val=20)
    brand_ratio = get_number("Brand mention percentage", default=30, min_val=10, max_val=50) / 100

    print(f"\n  Generating {num_comments} comments for live post...")
    print(f"  Brand: {brand['name']} | Mention ratio: {int(brand_ratio*100)}%")

    saved = comment_gen.generate_for_existing_post(
        reddit_url=url,
        subreddit_id=sub["id"],
        brand=brand,
        num_comments=num_comments,
        brand_mention_ratio=brand_ratio,
    )

    if not saved:
        print("  Generation failed.")
        return

    # Display results
    brand_count = sum(1 for c in saved if c.get("mentions_brand"))
    print(f"\n  Generated {len(saved)} comments:")
    print(f"    Brand mentions: {brand_count}/{len(saved)}")

    for i, c in enumerate(saved, 1):
        brand_tag = " [BRAND]" if c.get("mentions_brand") else ""
        reply_tag = f" (reply to u/{c['reply_to']})" if c.get("reply_to") else ""
        print(f"    {i}.{brand_tag}{reply_tag} \"{c['body'][:70]}...\"")

    # Link URL if not already linked
    db.add_post_url(sub["id"], url)
    print(f"\n  All comments saved. URL linked to r/{sub['name']}.")


def view_comments_flow(db: Database):
    """Browse generated comment trees per post."""
    subreddits = db.list_subreddits()
    sub = select_from_list(subreddits, lambda s: f"r/{s['name']}", "Select subreddit")
    if not sub:
        return

    posts = db.get_posts(sub["id"])
    if not posts:
        print("  No posts.")
        return

    # Add comment counts to display
    for p in posts:
        comments = db.get_comments(p["id"])
        p["_comment_count"] = len(comments)

    posts_with_comments = [p for p in posts if p["_comment_count"] > 0]
    if not posts_with_comments:
        print("  No posts have comments yet.")
        return

    post = select_from_list(
        posts_with_comments,
        lambda p: f"\"{p['title'][:40]}\" ({p['_comment_count']} comments)",
        "Select post"
    )
    if not post:
        return

    tree = db.get_comment_tree(post["id"])
    print(f"\n  Comment tree for \"{post['title'][:50]}\":")
    display_comment_tree(tree)

    # Option to view full comment text
    if confirm("View a comment in full?"):
        all_comments = db.get_comments(post["id"])
        comment = select_from_list(
            all_comments,
            lambda c: f"{'[REPLY]' if c['is_reply'] else '[TOP]'} {'[BRAND]' if c['mentions_brand'] else ''} \"{c['body'][:50]}\"",
            "Select comment"
        )
        if comment:
            print(f"\n  Full comment text:")
            print(f"  ---")
            print(f"  {comment['body']}")
            print(f"  ---")
            print(f"  Persona: {comment.get('persona_id', '?')} | Structure: {comment.get('structure_id', '?')}")
            print(f"  Day: {comment.get('suggested_post_day', 0)} | Status: {comment.get('status', '?')}")
            if comment.get('validation_score'):
                print(f"  Validation: {comment['validation_score']}/10")


def delete_regenerate_comment_flow(db: Database, comment_gen: CommentGenerator):
    """Delete or regenerate a specific comment."""
    subreddits = db.list_subreddits()
    sub = select_from_list(subreddits, lambda s: f"r/{s['name']}", "Select subreddit")
    if not sub:
        return

    posts = db.get_posts(sub["id"])
    post = select_from_list(posts, lambda p: f"\"{p['title'][:50]}\"", "Select post")
    if not post:
        return

    comments = db.get_comments(post["id"])
    if not comments:
        print("  No comments on this post.")
        return

    comment = select_from_list(
        comments,
        lambda c: f"{'[REPLY]' if c['is_reply'] else '[TOP]'} {'[BRAND]' if c['mentions_brand'] else ''} \"{c['body'][:50]}\"",
        "Select comment"
    )
    if not comment:
        return

    print_menu("Action", [("1", "Delete this comment"), ("2", "View full text")], back=True)
    action = get_choice(valid=["1", "2", "b"])

    if action == "1":
        if confirm("Delete this comment (and its replies)?"):
            db.delete_comment(comment["id"])
            print("  Comment deleted.")
    elif action == "2":
        print(f"\n  {comment['body']}")
