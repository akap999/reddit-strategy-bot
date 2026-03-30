"""Menu rendering and input helpers for the interactive CLI."""

import sys


def print_header(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_menu(title, options, back=True):
    """Print a menu with numbered options.

    Args:
        title: menu title
        options: list of (key, label) tuples, e.g. [("1", "Create New"), ("2", "View All")]
        back: whether to show [b] Back option
    """
    print(f"\n--- {title} ---")
    for key, label in options:
        print(f"  [{key}] {label}")
    if back:
        print(f"  [b] Back")
    print()


def get_choice(prompt="  > ", valid=None):
    """Get user input. If valid is a list, only accept those values."""
    while True:
        try:
            choice = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n")
            return "q"
        if valid is None or choice in valid:
            return choice
        print(f"  Invalid choice. Options: {', '.join(valid)}")


def get_input(prompt, required=True, default=None):
    """Get text input from user."""
    display = prompt
    if default:
        display = f"{prompt} [{default}]"
    try:
        value = input(f"  {display}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n")
        return default or ""
    if not value and default:
        return default
    if required and not value:
        print("  This field is required.")
        return get_input(prompt, required, default)
    return value


def get_number(prompt, default=None, min_val=1, max_val=100):
    """Get a number from user input."""
    display = prompt
    if default:
        display = f"{prompt} [{default}]"
    try:
        value = input(f"  {display}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n")
        return default or min_val
    if not value and default:
        return default
    try:
        num = int(value)
        if min_val <= num <= max_val:
            return num
        print(f"  Please enter a number between {min_val} and {max_val}.")
        return get_number(prompt, default, min_val, max_val)
    except ValueError:
        print("  Please enter a valid number.")
        return get_number(prompt, default, min_val, max_val)


def confirm(prompt, default=False):
    """Ask a yes/no question."""
    suffix = "[y/N]" if not default else "[Y/n]"
    try:
        value = input(f"  {prompt} {suffix}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n")
        return default
    if not value:
        return default
    return value in ("y", "yes")


def select_from_list(items, label_fn, title="Select an option"):
    """Display a numbered list and let user select one.

    Args:
        items: list of items
        label_fn: function that takes an item and returns its display label
        title: prompt title

    Returns:
        selected item or None if user goes back
    """
    if not items:
        print("  No items available.")
        return None

    print(f"\n  {title}:")
    for i, item in enumerate(items, 1):
        print(f"    {i}. {label_fn(item)}")
    print(f"    [b] Back")

    valid = [str(i) for i in range(1, len(items) + 1)] + ["b"]
    choice = get_choice("  > ", valid)
    if choice == "b":
        return None
    return items[int(choice) - 1]


def display_post(post, index=None):
    """Display a post summary."""
    prefix = f"Post {index}: " if index else ""
    status_icon = {"draft": "[DRAFT]", "complete": "[READY]", "published": "[PUBLISHED]"}.get(post.get("status", ""), "")
    filler = " (FILLER)" if post.get("is_filler") else ""
    print(f"  {prefix}{status_icon}{filler} \"{post['title'][:60]}\"")
    print(f"    Storyline: {post.get('storyline', '?')} | AI-query: {post.get('ai_query_score', '?')}/10 | Day: {post.get('suggested_post_day', 0)}")
    if post.get("image_prompt"):
        print(f"    Image: [{post['image_prompt'][:50]}...]")


def display_comment_tree(tree):
    """Display a comment tree with indentation."""
    for node in tree:
        brand_tag = " [BRAND]" if node.get("mentions_brand") else ""
        persona = node.get("persona_id", "?")
        day = node.get("suggested_post_day", 0)
        print(f"  [Day {day}] ({persona}){brand_tag}: \"{node['body'][:80]}...\"")
        for reply in node.get("replies", []):
            brand_tag_r = " [BRAND]" if reply.get("mentions_brand") else ""
            persona_r = reply.get("persona_id", "?")
            day_r = reply.get("suggested_post_day", 0)
            print(f"    [Day {day_r}] reply ({persona_r}){brand_tag_r}: \"{reply['body'][:70]}...\"")
