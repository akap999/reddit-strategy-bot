"""
Auto-assignment of Reddit accounts to draft comments.

Scoring algorithm evaluates every (account, comment) pair and assigns
the highest-scoring account to each draft comment, respecting Reddit
safety rules: subreddit cooldown, brand diversity, load balancing, etc.
"""

import time
from collections import defaultdict


def _build_lookups(context):
    """Pre-process raw DB rows into O(1)-lookup dicts."""
    # sub_day_counts[account_id][day] = count
    sub_day = defaultdict(lambda: defaultdict(int))
    for r in context["subreddit_day_assignments"]:
        sub_day[r["account_id"]][r["suggested_post_day"]] += r["cnt"]

    # pending[account_id] = count
    pending = defaultdict(int)
    for r in context["account_pending_counts"]:
        pending[r["account_id"]] = r["cnt"]

    # brand_mentions[account_id][brand_id] = count
    brand_mentions = defaultdict(lambda: defaultdict(int))
    for r in context["account_brand_mentions"]:
        brand_mentions[r["account_id"]][r["brand_id"]] = r["cnt"]

    # total_mentions[account_id] = count
    total_mentions = defaultdict(int)
    for r in context["account_total_mentions"]:
        total_mentions[r["account_id"]] = r["cnt"]

    return {
        "sub_day": sub_day,
        "pending": pending,
        "brand_mentions": brand_mentions,
        "total_mentions": total_mentions,
        "veterans": context["subreddit_veterans"],
    }


def score_account(account, comment, lookups, batch_picks):
    """Score an (account, comment) pair. Higher = better fit."""
    username = account["username"]
    score = 100

    # --- Subreddit cooldown: -30 per assignment in same sub within +/-2 days ---
    comment_day = comment.get("suggested_post_day", 0) or 0
    sub_day = lookups["sub_day"]
    for day_offset in range(-2, 3):
        score -= 30 * sub_day[username].get(comment_day + day_offset, 0)

    # --- Load balancing: -20 per pending assignment globally ---
    score -= 20 * lookups["pending"].get(username, 0)

    # --- Brand skew: proportional penalty starting at 25% concentration ---
    brand_id = comment.get("brand_id")
    if brand_id and comment.get("mentions_brand"):
        total = lookups["total_mentions"].get(username, 0)
        if total > 0:
            ratio = lookups["brand_mentions"][username].get(brand_id, 0) / total
            if ratio > 0.25:
                score -= int(80 * (ratio - 0.25))

    # --- Batch spread: -15 per pick already made in this run ---
    score -= 15 * batch_picks.get(username, 0)

    # --- Subreddit familiarity: +10 if account has history here ---
    if username in lookups["veterans"]:
        score += 10

    # --- Karma bonus: +5 per 1000 karma, capped at +25 ---
    total_karma = (account.get("link_karma") or 0) + (account.get("comment_karma") or 0)
    score += min(25, 5 * (total_karma // 1000))

    # --- Age bonus: +5 if account > 1 year old ---
    created = account.get("created_utc")
    if created and (time.time() - created) > 365 * 86400:
        score += 5

    return score


def _build_post_lookups(context):
    """Pre-process raw DB rows for post assignment scoring."""
    # Global post ownership count per account
    global_posts = defaultdict(int)
    for r in context["account_post_counts"]:
        global_posts[r["account_id"]] = r["cnt"]

    # Post ownership count in THIS subreddit per account
    sub_posts = defaultdict(int)
    for r in context["account_sub_post_counts"]:
        sub_posts[r["account_id"]] = r["cnt"]

    # Comment activity in this subreddit per account
    sub_comments = defaultdict(int)
    for r in context["account_sub_comment_counts"]:
        sub_comments[r["account_id"]] = r["cnt"]

    return {
        "global_posts": global_posts,
        "sub_posts": sub_posts,
        "sub_comments": sub_comments,
    }


def score_account_for_post(account, lookups, batch_picks, sub_owner):
    """Score an account for post ownership. Higher = better fit."""
    username = account["username"]
    score = 100

    # --- Subreddit post concentration: -25 per post already owned in this sub ---
    score -= 25 * lookups["sub_posts"].get(username, 0)

    # --- Global post load: -10 per post owned across all subs ---
    score -= 10 * lookups["global_posts"].get(username, 0)

    # --- Batch spread: -20 per pick already made in this run ---
    score -= 20 * batch_picks.get(username, 0)

    # --- Subreddit activity: +10 if account has comment activity here ---
    if lookups["sub_comments"].get(username, 0) > 0:
        score += 10

    # --- Subreddit owner bonus: +15 so they get some posts but not all ---
    if username == sub_owner:
        score += 15

    # --- Karma bonus: +5 per 1000 karma, capped at +25 ---
    total_karma = (account.get("link_karma") or 0) + (account.get("comment_karma") or 0)
    score += min(25, 5 * (total_karma // 1000))

    # --- Age bonus: +5 if account > 1 year old ---
    created = account.get("created_utc")
    if created and (time.time() - created) > 365 * 86400:
        score += 5

    return score


def auto_assign_posts(db, subreddit_id, exclude_accounts=None):
    """
    Auto-assign accounts to unowned posts in a subreddit.

    Returns dict with: assigned, assignments[], warnings[]
    """
    context = db.get_post_auto_assign_context(subreddit_id)
    if not context:
        return {"error": f"Subreddit {subreddit_id} not found"}

    sub = context["subreddit"]
    draft_posts = context["draft_posts"]
    accounts = context["all_accounts"]

    # Filter out excluded accounts
    if exclude_accounts:
        exclude_set = set(exclude_accounts)
        accounts = [a for a in accounts if a["username"] not in exclude_set]

    if not draft_posts:
        return {"assigned": 0, "assignments": [], "warnings": []}

    if not accounts:
        return {"error": "No accounts available for assignment"}

    lookups = _build_post_lookups(context)
    sub_owner = sub.get("owner_account") or ""
    batch_picks = defaultdict(int)
    assignments = []
    warnings = []

    for post in draft_posts:
        scores = [
            (score_account_for_post(a, lookups, batch_picks, sub_owner), a)
            for a in accounts
        ]
        scores.sort(key=lambda x: -x[0])
        best_score, best_account = scores[0]

        if best_score < 0:
            warnings.append(
                f"Post #{post['id']}: low confidence score ({best_score}). "
                f"Consider adding more accounts."
            )

        username = best_account["username"]
        db.set_post_owner(post["id"], username)
        batch_picks[username] += 1
        lookups["sub_posts"][username] = lookups["sub_posts"].get(username, 0) + 1
        lookups["global_posts"][username] = lookups["global_posts"].get(username, 0) + 1

        assignments.append({
            "post_id": post["id"],
            "account": username,
            "score": best_score,
        })

    return {
        "assigned": len(assignments),
        "assignments": assignments,
        "warnings": warnings,
    }


def auto_assign_post(db, post_id, exclude_accounts=None):
    """
    Auto-assign all draft comments for a post to accounts.

    Returns dict with: assigned, skipped, assignments[], warnings[]
    """
    context = db.get_auto_assign_context(post_id)
    if not context:
        return {"error": f"Post {post_id} not found"}

    post = context["post"]
    draft_comments = context["draft_comments"]
    accounts = context["all_accounts"]

    # Filter out excluded accounts
    if exclude_accounts:
        exclude_set = set(exclude_accounts)
        accounts = [a for a in accounts if a["username"] not in exclude_set]

    if not draft_comments:
        return {"assigned": 0, "skipped": 0, "assignments": [], "warnings": []}

    if not accounts:
        return {"error": "No accounts available for assignment"}

    lookups = _build_lookups(context)
    batch_picks = defaultdict(int)
    assignments = []
    warnings = []

    # Separate OP replies from regular comments
    op_comments = [c for c in draft_comments if c.get("comment_type") == "op_reply"]
    regular_comments = [c for c in draft_comments if c.get("comment_type") != "op_reply"]

    # --- Assign OP replies: all go to one account ---
    if op_comments:
        op_account = post.get("owner_account") or ""
        # If no owner_account set, or owner is excluded, pick best-scoring account
        valid_op = op_account and any(a["username"] == op_account for a in accounts)
        if not valid_op:
            # Check if any OP replies in this post are already assigned
            existing_op = db.conn.execute(
                """SELECT account_id FROM comments
                   WHERE post_id = ? AND comment_type = 'op_reply'
                     AND account_id IS NOT NULL AND account_id != ''
                   LIMIT 1""",
                (post["id"],)
            ).fetchone()
            if existing_op and any(a["username"] == existing_op["account_id"] for a in accounts):
                op_account = existing_op["account_id"]
            else:
                # Score all accounts for the first OP comment and pick best
                scores = [(score_account(a, op_comments[0], lookups, batch_picks), a) for a in accounts]
                scores.sort(key=lambda x: -x[0])
                op_account = scores[0][1]["username"]

        # If post has no owner, set the OP account as post owner
        if not post.get("owner_account"):
            db.set_post_owner(post["id"], op_account)

        for c in op_comments:
            db.assign_comment(c["id"], op_account)
            batch_picks[op_account] += 1
            # Also update pending in lookups so subsequent scoring reflects this
            lookups["pending"][op_account] = lookups["pending"].get(op_account, 0) + 1
            assignments.append({
                "comment_id": c["id"],
                "account": op_account,
                "score": None,
                "type": "op_reply",
            })

    # --- Assign regular comments (brand-mention first from query ordering) ---
    for comment in regular_comments:
        scores = [(score_account(a, comment, lookups, batch_picks), a) for a in accounts]
        scores.sort(key=lambda x: -x[0])
        best_score, best_account = scores[0]

        if best_score < 0:
            warnings.append(
                f"Comment #{comment['id']}: low confidence score ({best_score}). "
                f"Consider adding more accounts."
            )

        username = best_account["username"]
        db.assign_comment(comment["id"], username)
        batch_picks[username] += 1
        lookups["pending"][username] = lookups["pending"].get(username, 0) + 1

        # Update sub_day lookups so next comment sees this assignment
        comment_day = comment.get("suggested_post_day", 0) or 0
        lookups["sub_day"][username][comment_day] += 1

        assignments.append({
            "comment_id": comment["id"],
            "account": username,
            "score": best_score,
            "type": "brand" if comment.get("mentions_brand") else "organic",
        })

    return {
        "assigned": len(assignments),
        "skipped": 0,
        "assignments": assignments,
        "warnings": warnings,
    }


def auto_assign_single_comment(db, comment_id, exclude_accounts=None):
    """Auto-assign a single unassigned comment to the best-scoring account."""
    comment = db.get_comment(comment_id)
    if not comment:
        return {"error": "Comment not found"}
    if comment.get("account_id") and comment["status"] not in ("draft", "complete"):
        return {"error": "Comment already assigned"}

    post_id = comment["post_id"]
    context = db.get_auto_assign_context(post_id)
    if not context:
        return {"error": "Post not found"}

    accounts = context["all_accounts"]
    if exclude_accounts:
        accounts = [a for a in accounts if a["username"] not in set(exclude_accounts)]
    if not accounts:
        return {"error": "No accounts available"}

    lookups = _build_lookups(context)
    batch_picks = defaultdict(int)

    # OP reply: prefer post owner, then match existing OP assignments
    post = context["post"]
    if comment.get("comment_type") == "op_reply":
        op_acct = post.get("owner_account") or ""
        # If no post owner, check for existing OP reply assignments in this post
        if not op_acct or not any(a["username"] == op_acct for a in accounts):
            existing_op = db.conn.execute(
                """SELECT account_id FROM comments
                   WHERE post_id = ? AND comment_type = 'op_reply'
                     AND account_id IS NOT NULL AND account_id != ''
                   LIMIT 1""",
                (post_id,)
            ).fetchone()
            if existing_op and any(a["username"] == existing_op["account_id"] for a in accounts):
                op_acct = existing_op["account_id"]
        if op_acct and any(a["username"] == op_acct for a in accounts):
            db.assign_comment(comment_id, op_acct)
            # Also set post owner if not set
            if not post.get("owner_account"):
                db.set_post_owner(post_id, op_acct)
            return {"ok": True, "account": op_acct, "score": None, "type": "op_reply"}

    scores = [(score_account(a, comment, lookups, batch_picks), a) for a in accounts]
    scores.sort(key=lambda x: -x[0])
    best_score, best_account = scores[0]
    username = best_account["username"]
    db.assign_comment(comment_id, username)
    return {"ok": True, "account": username, "score": best_score}


def auto_assign_single_post(db, post_id, exclude_accounts=None):
    """Auto-assign a single unowned post to the best-scoring account."""
    post = db.get_post(post_id)
    if not post:
        return {"error": "Post not found"}
    if post.get("owner_account"):
        return {"error": "Post already has an owner"}

    subreddit_id = post["subreddit_id"]
    context = db.get_post_auto_assign_context(subreddit_id)
    if not context:
        return {"error": "Subreddit not found"}

    accounts = context["all_accounts"]
    if exclude_accounts:
        accounts = [a for a in accounts if a["username"] not in set(exclude_accounts)]
    if not accounts:
        return {"error": "No accounts available"}

    lookups = _build_post_lookups(context)
    sub_owner = context["subreddit"].get("owner_account") or ""
    batch_picks = defaultdict(int)

    scores = [(score_account_for_post(a, lookups, batch_picks, sub_owner), a) for a in accounts]
    scores.sort(key=lambda x: -x[0])
    best_score, best_account = scores[0]
    username = best_account["username"]
    db.set_post_owner(post_id, username)
    return {"ok": True, "account": username, "score": best_score}
