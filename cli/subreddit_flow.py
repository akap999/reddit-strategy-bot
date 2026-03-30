"""Interactive flows for subreddit creation and brand management."""

import json
from cli.menus import (
    print_header, print_menu, get_choice, get_input, get_number,
    confirm, select_from_list
)
from generators.subreddit_gen import SubredditGenerator
from generators.comment_gen import CommentGenerator
from db import Database


def subreddit_menu(db: Database, sub_gen: SubredditGenerator, comment_gen: CommentGenerator):
    """Main subreddit management menu."""
    while True:
        print_menu("Manage Subreddits", [
            ("1", "Create New Subreddit"),
            ("2", "Add Brand to Subreddit"),
            ("3", "Link Reddit URL to Subreddit"),
            ("4", "Mark Post as Published"),
            ("5", "View All Subreddits"),
        ])
        choice = get_choice(valid=["1", "2", "3", "4", "5", "b"])

        if choice == "b":
            return
        elif choice == "1":
            create_subreddit_flow(db, sub_gen, comment_gen)
        elif choice == "2":
            add_brand_flow(db, comment_gen)
        elif choice == "3":
            link_url_flow(db)
        elif choice == "4":
            mark_published_flow(db)
        elif choice == "5":
            view_subreddits(db)


def create_subreddit_flow(db: Database, sub_gen: SubredditGenerator, comment_gen: CommentGenerator):
    """Flow: input brands -> generate subreddit name suggestions -> create subreddit."""
    print_header("Create New Subreddit")

    # Collect brand references
    brand_names_input = get_input("Enter brand name(s) as reference (comma-separated)")
    brand_names = [b.strip() for b in brand_names_input.split(",") if b.strip()]
    if not brand_names:
        print("  No brands entered.")
        return

    # Get brand contexts
    brand_contexts = []
    for name in brand_names:
        # Try to auto-extract
        domain = get_input(f"Domain for {name} (optional, e.g. example.com)", required=False)
        if domain:
            print(f"    Extracting brand info from {domain}...")
            info = comment_gen.extract_brand_info(domain)
            if info:
                print(f"    Found: {info['brand_context']}")
                brand_contexts.append(info["brand_context"])
                continue
        ctx = get_input(f"What does {name} do?")
        brand_contexts.append(ctx)

    # Generate name suggestions
    print("\n  Generating subreddit name suggestions...")
    suggestions = sub_gen.generate_names(brand_names, brand_contexts)

    if not suggestions:
        print("  Failed to generate suggestions.")
        return

    # Display suggestions with availability
    print("\n  Subreddit Name Suggestions:")
    for i, s in enumerate(suggestions, 1):
        avail = "[AVAILABLE]" if s["available"] is True else ("[TAKEN]" if s["available"] is False else "[UNKNOWN]")
        print(f"    {i}. {s['name']:25s} {avail:12s} - {s['description']}")
    print(f"    {len(suggestions)+1}. [Enter custom name]")

    valid = [str(i) for i in range(1, len(suggestions) + 2)] + ["b"]
    choice = get_choice("  Select: ", valid)
    if choice == "b":
        return

    if int(choice) == len(suggestions) + 1:
        chosen_name = get_input("Enter custom subreddit name")
    else:
        chosen_name = suggestions[int(choice) - 1]["name"]

    # Check if already exists in DB
    if db.get_subreddit_by_name(chosen_name):
        print(f"  Subreddit r/{chosen_name} already exists in the database.")
        return

    # Determine domain
    domain = get_input("Topic domain (e.g., 'men's telehealth', 'hormone therapy')")

    # Generate subreddit info
    print(f"\n  Generating info for r/{chosen_name}...")
    info = sub_gen.generate_subreddit_info(chosen_name, domain)

    if not info:
        print("  Failed to generate subreddit info.")
        return

    # Display generated info
    print(f"\n  Description: {info.get('description', 'N/A')}")
    rules = info.get("rules", [])
    if rules:
        print(f"  Rules ({len(rules)}):")
        for r in rules[:5]:
            print(f"    - {r.get('title', '?')}: {r.get('description', '')[:60]}...")
    print(f"  Sidebar: {info.get('sidebar', 'N/A')[:100]}...")
    print(f"  Welcome: {info.get('welcome_message', 'N/A')[:100]}...")

    if not confirm("Create this subreddit?"):
        return

    # Save to DB
    sub_id = db.create_subreddit(
        name=chosen_name,
        domain=domain,
        description=info.get("description", ""),
        rules=info.get("rules_json", "[]"),
        sidebar=info.get("sidebar", ""),
        welcome_message=info.get("welcome_message", ""),
    )
    print(f"\n  Subreddit r/{chosen_name} created (ID: {sub_id})")

    # Offer to add first brand
    if confirm("Add a brand to this subreddit now?", default=True):
        _add_brand_to_subreddit(db, sub_id, comment_gen, brand_names, brand_contexts)


def add_brand_flow(db: Database, comment_gen: CommentGenerator):
    """Add a brand to an existing subreddit."""
    subreddits = db.list_subreddits()
    sub = select_from_list(subreddits, lambda s: f"r/{s['name']} ({s['brand_count']} brands)", "Select subreddit")
    if not sub:
        return
    _add_brand_to_subreddit(db, sub["id"], comment_gen)


def _add_brand_to_subreddit(db, subreddit_id, comment_gen, preset_names=None, preset_contexts=None):
    """Internal: add one or more brands to a subreddit."""
    names = preset_names or []
    contexts = preset_contexts or []

    if not names:
        name = get_input("Brand name")
        names = [name]

    for i, name in enumerate(names):
        # Check if already exists
        if db.get_brand_by_name(subreddit_id, name):
            print(f"  Brand '{name}' already exists in this subreddit.")
            continue

        context = contexts[i] if i < len(contexts) else ""
        domain_url = ""
        keywords = "[]"

        if not context:
            domain_url = get_input(f"Domain for {name} (optional)", required=False)
            if domain_url:
                info = comment_gen.extract_brand_info(domain_url)
                if info:
                    context = info.get("brand_context", "")
                    kw = info.get("brand_keywords", [])
                    keywords = json.dumps(kw)
                    print(f"    Context: {context}")
                    print(f"    Keywords: {', '.join(kw)}")
            if not context:
                context = get_input(f"What does {name} do?")

        kw_input = get_input(f"Keywords for {name} (comma-separated, optional)", required=False)
        if kw_input:
            keywords = json.dumps([k.strip() for k in kw_input.split(",")])

        brand_id = db.add_brand(
            subreddit_id=subreddit_id,
            name=name,
            domain_url=domain_url,
            context=context,
            keywords=keywords,
        )
        print(f"  Brand '{name}' added (ID: {brand_id})")


def link_url_flow(db: Database):
    """Link a Reddit post URL to a subreddit for live comment analysis."""
    subreddits = db.list_subreddits()
    sub = select_from_list(subreddits, lambda s: f"r/{s['name']}", "Select subreddit")
    if not sub:
        return

    url = get_input("Reddit post URL")
    if "reddit.com" not in url:
        print("  Invalid Reddit URL.")
        return

    db.add_post_url(sub["id"], url)
    print(f"  URL linked to r/{sub['name']}")


def mark_published_flow(db: Database):
    """Mark a generated post as published and link its Reddit URL."""
    subreddits = db.list_subreddits()
    sub = select_from_list(subreddits, lambda s: f"r/{s['name']}", "Select subreddit")
    if not sub:
        return

    posts = db.get_posts(sub["id"])
    unpublished = [p for p in posts if p["status"] != "published"]
    if not unpublished:
        print("  No unpublished posts.")
        return

    post = select_from_list(unpublished, lambda p: f"[{p['status']}] \"{p['title'][:50]}\"", "Select post")
    if not post:
        return

    url = get_input("Reddit URL where this was posted")
    if "reddit.com" not in url:
        print("  Invalid Reddit URL.")
        return

    db.update_post_status(post["id"], "published")
    db.link_url_to_post(post["id"], url, sub["id"])
    print(f"  Post marked as published and linked to URL.")


def view_subreddits(db: Database):
    """Display all subreddits with their brands."""
    subreddits = db.list_subreddits()
    if not subreddits:
        print("  No subreddits yet.")
        return

    for sub in subreddits:
        print(f"\n  r/{sub['name']} (domain: {sub['domain']})")
        print(f"    Brands: {sub['brand_count']} | Posts: {sub['post_count']} | Comments: {sub['comment_count']}")
        brands = db.list_brands(sub["id"])
        for b in brands:
            kw = json.loads(b.get("keywords", "[]")) if b.get("keywords") else []
            kw_text = f" ({', '.join(kw[:3])})" if kw else ""
            print(f"      - {b['name']}{kw_text}: {b['context'][:60]}")
