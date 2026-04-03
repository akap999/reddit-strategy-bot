"""
Auto-assignment of Reddit accounts to draft comments.

Hard rules (never violated):
  1. Reply comments (is_reply=1) → SAME account as their parent comment
  2. OP replies (comment_type='op_reply') → post owner only
  3. Top-level non-OP comments → unique account per post, never the post owner
  4. Already-assigned / deployed comments are never touched

Soft scoring picks the best account among those that pass the hard rules.
"""

import time
from collections import defaultdict


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def _build_lookups(context):
    """Pre-process raw DB rows into O(1)-lookup dicts."""
    sub_day = defaultdict(lambda: defaultdict(int))
    for r in context["subreddit_day_assignments"]:
        sub_day[r["account_id"]][r["suggested_post_day"]] += r["cnt"]

    pending = defaultdict(int)
    for r in context["account_pending_counts"]:
        pending[r["account_id"]] = r["cnt"]

    brand_mentions = defaultdict(lambda: defaultdict(int))
    for r in context["account_brand_mentions"]:
        brand_mentions[r["account_id"]][r["brand_id"]] = r["cnt"]

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


def _get_post_toplevel_accounts(db, post_id):
    """Return set of account usernames that already have a TOP-LEVEL
    (non-reply) comment (assigned, informed, or deployed) on this post.
    Replies inherit their parent's account, so they don't count toward
    the one-account-per-post rule."""
    rows = db.conn.execute(
        """SELECT DISTINCT account_id FROM comments
           WHERE post_id = ? AND account_id IS NOT NULL AND account_id != ''
             AND status IN ('assigned', 'informed', 'deployed')
             AND is_reply = 0""",
        (post_id,)
    ).fetchall()
    return {r["account_id"] for r in rows}


def _get_parent_account(db, parent_comment_id):
    """Get the account_id of a parent comment. Returns None if not found
    or parent is unassigned."""
    if not parent_comment_id:
        return None
    row = db.conn.execute(
        "SELECT account_id FROM comments WHERE id = ?",
        (parent_comment_id,)
    ).fetchone()
    if row and row["account_id"]:
        return row["account_id"]
    return None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_account(account, comment, lookups, batch_picks):
    """Score an (account, comment) pair. Higher = better fit."""
    username = account["username"]
    score = 100

    # Subreddit cooldown: -30 per assignment in same sub within ±2 days
    comment_day = comment.get("suggested_post_day", 0) or 0
    sub_day = lookups["sub_day"]
    for day_offset in range(-2, 3):
        score -= 30 * sub_day[username].get(comment_day + day_offset, 0)

    # Load balancing: -20 per pending assignment globally
    score -= 20 * lookups["pending"].get(username, 0)

    # Brand skew: proportional penalty starting at 25% concentration
    brand_id = comment.get("brand_id")
    if brand_id and comment.get("mentions_brand"):
        total = lookups["total_mentions"].get(username, 0)
        if total > 0:
            ratio = lookups["brand_mentions"][username].get(brand_id, 0) / total
            if ratio > 0.25:
                score -= int(80 * (ratio - 0.25))

    # Batch spread: -15 per pick already made in this run
    score -= 15 * batch_picks.get(username, 0)

    # Subreddit familiarity: +10 if account has history here
    if username in lookups["veterans"]:
        score += 10

    # Karma bonus: +5 per 1000 karma, capped at +25
    total_karma = (account.get("link_karma") or 0) + (account.get("comment_karma") or 0)
    score += min(25, 5 * (total_karma // 1000))

    # Age bonus: +5 if account > 1 year old
    created = account.get("created_utc")
    if created and (time.time() - created) > 365 * 86400:
        score += 5

    return score


# ---------------------------------------------------------------------------
# Post ownership scoring
# ---------------------------------------------------------------------------

def _build_post_lookups(context):
    global_posts = defaultdict(int)
    for r in context["account_post_counts"]:
        global_posts[r["account_id"]] = r["cnt"]

    sub_posts = defaultdict(int)
    for r in context["account_sub_post_counts"]:
        sub_posts[r["account_id"]] = r["cnt"]

    sub_comments = defaultdict(int)
    for r in context["account_sub_comment_counts"]:
        sub_comments[r["account_id"]] = r["cnt"]

    return {
        "global_posts": global_posts,
        "sub_posts": sub_posts,
        "sub_comments": sub_comments,
    }


def score_account_for_post(account, lookups, batch_picks, sub_owner):
    username = account["username"]
    score = 100
    score -= 25 * lookups["sub_posts"].get(username, 0)
    score -= 10 * lookups["global_posts"].get(username, 0)
    score -= 20 * batch_picks.get(username, 0)
    if lookups["sub_comments"].get(username, 0) > 0:
        score += 10
    if username == sub_owner:
        score += 15
    total_karma = (account.get("link_karma") or 0) + (account.get("comment_karma") or 0)
    score += min(25, 5 * (total_karma // 1000))
    created = account.get("created_utc")
    if created and (time.time() - created) > 365 * 86400:
        score += 5
    return score


# ---------------------------------------------------------------------------
# Bulk post ownership assignment
# ---------------------------------------------------------------------------

def auto_assign_posts(db, subreddit_id, exclude_accounts=None):
    context = db.get_post_auto_assign_context(subreddit_id)
    if not context:
        return {"error": f"Subreddit {subreddit_id} not found"}

    sub = context["subreddit"]
    draft_posts = context["draft_posts"]
    accounts = context["all_accounts"]

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


# ---------------------------------------------------------------------------
# Bulk comment assignment for a single post
# ---------------------------------------------------------------------------

def auto_assign_post(db, post_id, exclude_accounts=None):
    """
    Auto-assign all draft comments for a post to accounts.

    Hard rules enforced:
      - Reply comments → same account as parent comment
      - OP replies → post owner only
      - Top-level non-OP → unique account per post, never the post owner
    """
    context = db.get_auto_assign_context(post_id)
    if not context:
        return {"error": f"Post {post_id} not found"}

    post = context["post"]
    draft_comments = context["draft_comments"]
    accounts = context["all_accounts"]

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

    # Track accounts used for top-level comments on this post (from prior runs)
    used_on_post = _get_post_toplevel_accounts(db, post_id)

    # Categorize comments into three groups
    op_comments = []
    reply_comments = []
    toplevel_comments = []
    for c in draft_comments:
        if c.get("comment_type") == "op_reply":
            op_comments.append(c)
        elif c.get("is_reply") and c.get("parent_comment_id"):
            reply_comments.append(c)
        else:
            toplevel_comments.append(c)

    # ---------------------------------------------------------------
    # 1. OP replies: ALL go to the post owner
    # ---------------------------------------------------------------
    op_account = post.get("owner_account") or ""

    if op_comments:
        valid_op = op_account and any(a["username"] == op_account for a in accounts)
        if not valid_op:
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
                scores = [(score_account(a, op_comments[0], lookups, batch_picks), a) for a in accounts]
                scores.sort(key=lambda x: -x[0])
                op_account = scores[0][1]["username"]

        if not post.get("owner_account"):
            db.set_post_owner(post["id"], op_account)

        used_on_post.add(op_account)

        for c in op_comments:
            db.assign_comment(c["id"], op_account)
            batch_picks[op_account] += 1
            lookups["pending"][op_account] = lookups["pending"].get(op_account, 0) + 1
            assignments.append({
                "comment_id": c["id"],
                "account": op_account,
                "score": None,
                "type": "op_reply",
            })

    # ---------------------------------------------------------------
    # 2. Reply comments: inherit parent's account
    # ---------------------------------------------------------------
    # Build a map of comment_id → account from already-assigned comments
    # (includes ones we just assigned above + existing DB state)
    assigned_map = {}  # comment_id → account_id
    for a in assignments:
        assigned_map[a["comment_id"]] = a["account"]

    skipped = 0
    for c in reply_comments:
        parent_acct = _get_parent_account(db, c["parent_comment_id"])
        # Also check if parent was just assigned in this batch
        if not parent_acct:
            parent_acct = assigned_map.get(c["parent_comment_id"])

        if parent_acct:
            db.assign_comment(c["id"], parent_acct)
            assigned_map[c["id"]] = parent_acct
            batch_picks[parent_acct] += 1
            lookups["pending"][parent_acct] = lookups["pending"].get(parent_acct, 0) + 1
            assignments.append({
                "comment_id": c["id"],
                "account": parent_acct,
                "score": None,
                "type": "reply",
            })
        else:
            skipped += 1
            warnings.append(
                f"Comment #{c['id']}: skipped — parent comment #{c['parent_comment_id']} "
                f"has no account assigned yet."
            )

    # ---------------------------------------------------------------
    # 3. Top-level non-OP: unique account per post, never the OP
    # ---------------------------------------------------------------
    op_to_exclude = op_account or post.get("owner_account") or ""
    blocked = set(used_on_post)
    if op_to_exclude:
        blocked.add(op_to_exclude)

    for comment in toplevel_comments:
        eligible = [a for a in accounts if a["username"] not in blocked]
        if not eligible:
            skipped += 1
            warnings.append(
                f"Comment #{comment['id']}: skipped — no unique account available "
                f"(all accounts already used on this post)."
            )
            continue

        scores = [(score_account(a, comment, lookups, batch_picks), a) for a in eligible]
        scores.sort(key=lambda x: -x[0])
        best_score, best_account = scores[0]

        if best_score < 0:
            warnings.append(
                f"Comment #{comment['id']}: low confidence score ({best_score}). "
                f"Consider adding more accounts."
            )

        username = best_account["username"]
        db.assign_comment(comment["id"], username)
        assigned_map[comment["id"]] = username
        batch_picks[username] += 1
        lookups["pending"][username] = lookups["pending"].get(username, 0) + 1

        comment_day = comment.get("suggested_post_day", 0) or 0
        lookups["sub_day"][username][comment_day] += 1

        # Mark this account as used on the post (top-level uniqueness)
        blocked.add(username)
        used_on_post.add(username)

        assignments.append({
            "comment_id": comment["id"],
            "account": username,
            "score": best_score,
            "type": "brand" if comment.get("mentions_brand") else "organic",
        })

    return {
        "assigned": len(assignments),
        "skipped": skipped,
        "assignments": assignments,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Single comment assignment
# ---------------------------------------------------------------------------

def auto_assign_single_comment(db, comment_id, exclude_accounts=None):
    """Auto-assign a single unassigned comment to the best-scoring account.

    Hard rules:
      - Reply → same account as parent
      - OP reply → post owner only
      - Top-level non-OP → unique account, never the post owner
    """
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

    post = context["post"]
    lookups = _build_lookups(context)
    batch_picks = defaultdict(int)

    # ---------------------------------------------------------------
    # Reply → must go to parent's account
    # ---------------------------------------------------------------
    if comment.get("is_reply") and comment.get("parent_comment_id"):
        parent_acct = _get_parent_account(db, comment["parent_comment_id"])
        if parent_acct:
            db.assign_comment(comment_id, parent_acct)
            return {"ok": True, "account": parent_acct, "score": None, "type": "reply"}
        return {"error": f"Parent comment #{comment['parent_comment_id']} has no account. Assign the parent first."}

    # ---------------------------------------------------------------
    # OP reply → must go to post owner
    # ---------------------------------------------------------------
    if comment.get("comment_type") == "op_reply":
        op_acct = post.get("owner_account") or ""
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
            if not post.get("owner_account"):
                db.set_post_owner(post_id, op_acct)
            return {"ok": True, "account": op_acct, "score": None, "type": "op_reply"}

        return {"error": "No valid OP account available. Assign a post owner first."}

    # ---------------------------------------------------------------
    # Top-level non-OP → unique account, exclude OP + already used
    # ---------------------------------------------------------------
    used_on_post = _get_post_toplevel_accounts(db, post_id)
    op_account_name = post.get("owner_account") or ""
    blocked = set(used_on_post)
    if op_account_name:
        blocked.add(op_account_name)

    eligible = [a for a in accounts if a["username"] not in blocked]
    if not eligible:
        return {"error": "No unique account available — all accounts already used on this post."}

    scores = [(score_account(a, comment, lookups, batch_picks), a) for a in eligible]
    scores.sort(key=lambda x: -x[0])
    best_score, best_account = scores[0]
    username = best_account["username"]
    db.assign_comment(comment_id, username)
    return {"ok": True, "account": username, "score": best_score}


# ---------------------------------------------------------------------------
# Single post ownership assignment
# ---------------------------------------------------------------------------

def auto_assign_single_post(db, post_id, exclude_accounts=None):
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
