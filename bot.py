#!/usr/bin/env python3
"""Reddit Strategy Bot — Interactive CLI entry point."""

import sys
import os
import csv
import json

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import ANTHROPIC_API_KEY, DB_PATH
from db import Database
from generators.base import ClaudeClient
from generators.subreddit_gen import SubredditGenerator
from generators.post_gen import PostGenerator
from generators.comment_gen import CommentGenerator
from cli.menus import print_header, print_menu, get_choice, get_input, confirm, select_from_list
from cli.subreddit_flow import subreddit_menu
from cli.post_flow import post_menu
from cli.comment_flow import comment_menu
from cli.analytics_flow import analytics_menu


def export_menu(db: Database):
    """Export data to CSV."""
    while True:
        print_menu("Export", [
            ("1", "Export Subreddit Data (posts + comments)"),
            ("2", "Export Posting Schedule"),
        ])
        choice = get_choice(valid=["1", "2", "b"])

        if choice == "b":
            return
        elif choice == "1":
            export_subreddit_data(db)
        elif choice == "2":
            export_schedule(db)


def export_subreddit_data(db: Database):
    """Export all posts and comments for a subreddit to CSV."""
    subreddits = db.list_subreddits()
    sub = select_from_list(subreddits, lambda s: f"r/{s['name']}", "Select subreddit")
    if not sub:
        return

    filename = get_input("Output filename", default=f"{sub['name']}_export.csv")
    posts = db.get_posts(sub["id"], include_filler=True)

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "type", "post_id", "post_title", "post_body", "storyline",
            "ai_query_score", "is_filler", "status", "day",
            "comment_id", "comment_body", "persona", "structure",
            "is_reply", "parent_comment_id", "mentions_brand",
            "comment_status", "comment_day", "image_prompt"
        ])

        for post in posts:
            # Write post row
            writer.writerow([
                "POST", post["id"], post["title"], post["body"],
                post["storyline"], post["ai_query_score"],
                post["is_filler"], post["status"], post["suggested_post_day"],
                "", "", "", "", "", "", "", "", "", post.get("image_prompt", "")
            ])

            # Write comment rows
            comments = db.get_comments(post["id"])
            for c in comments:
                writer.writerow([
                    "COMMENT", post["id"], post["title"], "",
                    "", "", "", "", "",
                    c["id"], c["body"], c.get("persona_id", ""),
                    c.get("structure_id", ""), c["is_reply"],
                    c.get("parent_comment_id", ""), c["mentions_brand"],
                    c["status"], c["suggested_post_day"], ""
                ])

    print(f"  Exported to {filename} ({len(posts)} posts)")


def export_schedule(db: Database):
    """Export the posting schedule to CSV."""
    subreddits = db.list_subreddits()
    sub = select_from_list(subreddits, lambda s: f"r/{s['name']}", "Select subreddit")
    if not sub:
        return

    filename = get_input("Output filename", default=f"{sub['name']}_schedule.csv")
    schedule = db.get_schedule_status(sub["id"])

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "day", "type", "title_or_body", "status", "is_filler",
            "mentions_brand", "persona", "is_reply", "image_prompt"
        ])

        for day in sorted(schedule.keys()):
            entries = schedule[day]
            for p in entries["posts"]:
                writer.writerow([
                    day, "POST", p["title"], p["status"],
                    p.get("is_filler", 0), "N/A", "N/A", "N/A",
                    p.get("image_prompt", "")
                ])
            for c in entries["comments"]:
                writer.writerow([
                    day, "COMMENT", c["body"][:200], c["status"],
                    "", c.get("mentions_brand", 0),
                    c.get("persona_id", ""), c.get("is_reply", 0), ""
                ])

    print(f"  Schedule exported to {filename}")


def main():
    # Check for legacy mode
    if len(sys.argv) > 1 and sys.argv[1] == "--legacy":
        print("  Running legacy CSV mode...")
        os.system(f"python {os.path.join(os.path.dirname(__file__), 'comment_generator.py')} {' '.join(sys.argv[2:])}")
        return

    # Check API key
    api_key = ANTHROPIC_API_KEY
    if not api_key:
        api_key = get_input("Enter Anthropic API key (or set ANTHROPIC_API_KEY env var)")
        if not api_key:
            print("  API key is required.")
            return
        os.environ["ANTHROPIC_API_KEY"] = api_key

    # Initialize database
    db = Database(DB_PATH)
    db.connect()
    db.initialize()

    # Initialize generators
    claude = ClaudeClient(api_key)
    sub_gen = SubredditGenerator(claude)
    post_gen = PostGenerator(claude, db)
    comment_gen = CommentGenerator(claude, db)

    print_header("Reddit Strategy Bot")
    print(f"  Database: {DB_PATH}")
    print(f"  Model: {claude.model}")

    # Quick stats
    subreddits = db.list_subreddits()
    total_brands = sum(s["brand_count"] for s in subreddits)
    total_posts = sum(s["post_count"] for s in subreddits)
    total_comments = sum(s["comment_count"] for s in subreddits)
    print(f"  {len(subreddits)} subreddits | {total_brands} brands | {total_posts} posts | {total_comments} comments")

    try:
        while True:
            print_menu("Main Menu", [
                ("1", "Manage Subreddits"),
                ("2", "Manage Posts"),
                ("3", "Manage Comments"),
                ("4", "Analytics Dashboard"),
                ("5", "Export"),
                ("q", "Quit"),
            ], back=False)
            choice = get_choice(valid=["1", "2", "3", "4", "5", "q"])

            if choice == "q":
                print("\n  Goodbye!")
                break
            elif choice == "1":
                subreddit_menu(db, sub_gen, comment_gen)
            elif choice == "2":
                post_menu(db, post_gen)
            elif choice == "3":
                comment_menu(db, comment_gen)
            elif choice == "4":
                analytics_menu(db)
            elif choice == "5":
                export_menu(db)
    except KeyboardInterrupt:
        print("\n\n  Interrupted. Goodbye!")
    finally:
        db.close()


if __name__ == "__main__":
    main()
