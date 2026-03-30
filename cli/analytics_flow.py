"""Interactive analytics dashboard CLI."""

from cli.menus import print_header, print_menu, get_choice, get_input, select_from_list
from db import Database
import analytics


def analytics_menu(db: Database):
    """Main analytics dashboard menu."""
    while True:
        print_menu("Analytics Dashboard", [
            ("1", "Subreddit Overview"),
            ("2", "Brand Performance"),
            ("3", "Content Health"),
            ("4", "Posting Schedule Status"),
        ])
        choice = get_choice(valid=["1", "2", "3", "4", "b"])

        if choice == "b":
            return
        elif choice == "1":
            subreddit_overview_flow(db)
        elif choice == "2":
            brand_performance_flow(db)
        elif choice == "3":
            content_health_flow(db)
        elif choice == "4":
            schedule_status_flow(db)


def subreddit_overview_flow(db: Database):
    """Select a subreddit and show overview stats."""
    print_header("Subreddit Overview")
    subreddits = db.list_subreddits()

    if not subreddits:
        print("  No subreddits yet.")
        return

    # Option to view all or select one
    if len(subreddits) == 1:
        analytics.subreddit_overview(db, subreddits[0]["id"])
    else:
        print_menu("View", [("1", "All subreddits"), ("2", "Select one")], back=True)
        choice = get_choice(valid=["1", "2", "b"])
        if choice == "b":
            return
        elif choice == "1":
            for sub in subreddits:
                analytics.subreddit_overview(db, sub["id"])
        elif choice == "2":
            sub = select_from_list(subreddits, lambda s: f"r/{s['name']}", "Select subreddit")
            if sub:
                analytics.subreddit_overview(db, sub["id"])


def brand_performance_flow(db: Database):
    """Show brand performance stats."""
    print_header("Brand Performance")

    # Collect all unique brand names
    subreddits = db.list_subreddits()
    all_brands = []
    seen = set()
    for sub in subreddits:
        brands = db.list_brands(sub["id"])
        for b in brands:
            if b["name"] not in seen:
                seen.add(b["name"])
                all_brands.append(b)

    if not all_brands:
        print("  No brands yet.")
        return

    if len(all_brands) == 1:
        analytics.brand_performance(db, all_brands[0]["name"])
    else:
        print_menu("View", [("1", "All brands"), ("2", "Select one")], back=True)
        choice = get_choice(valid=["1", "2", "b"])
        if choice == "b":
            return
        elif choice == "1":
            analytics.brand_performance(db)
        elif choice == "2":
            brand = select_from_list(all_brands, lambda b: b["name"], "Select brand")
            if brand:
                analytics.brand_performance(db, brand["name"])


def content_health_flow(db: Database):
    """Show content health analysis."""
    print_header("Content Health")
    subreddits = db.list_subreddits()
    sub = select_from_list(subreddits, lambda s: f"r/{s['name']}", "Select subreddit")
    if sub:
        analytics.content_health(db, sub["id"])


def schedule_status_flow(db: Database):
    """Show posting schedule status."""
    print_header("Posting Schedule Status")
    subreddits = db.list_subreddits()
    sub = select_from_list(subreddits, lambda s: f"r/{s['name']}", "Select subreddit")
    if sub:
        analytics.posting_schedule_status(db, sub["id"])
