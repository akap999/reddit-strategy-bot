"""
Auto-assignment of Reddit accounts to draft comments.

Hard rules (never violated):
  1. OP replies (comment_type='op_reply') → post owner only
  2. Top-level non-OP comments → unique account per post, never the post owner
  3. Reply comments (is_reply=1) → normal scoring, exempt from uniqueness + OP exclusion
  4. Already-assigned / deployed comments are never touched

Soft scoring picks the best account among those that pass the hard rules.
"""

import hashlib
import time
import random
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

    # Global deployed footprint
    deployed = defaultdict(int)
    for r in context.get("account_deployed_counts", []):
        deployed[r["account_id"]] = r["cnt"]

    # Post ownership count
    post_ownership = defaultdict(int)
    for r in context.get("account_post_ownership", []):
        post_ownership[r["account_id"]] = r["cnt"]

    # Subreddit spread (how many distinct subs the account is active in)
    sub_spread = defaultdict(int)
    for r in context.get("account_sub_spread", []):
        sub_spread[r["account_id"]] = r["cnt"]

    return {
        "sub_day": sub_day,
        "pending": pending,
        "brand_mentions": brand_mentions,
        "total_mentions": total_mentions,
        "veterans": context["subreddit_veterans"],
        "deployed": deployed,
        "post_ownership": post_ownership,
        "sub_spread": sub_spread,
    }


def _get_post_toplevel_accounts(db, post_id):
    """Return set of account usernames that already have a TOP-LEVEL
    (non-reply) comment (assigned, informed, or deployed) on this post.
    Only top-level comments count toward uniqueness — replies are exempt."""
    rows = db.conn.execute(
        """SELECT DISTINCT account_id FROM comments
           WHERE post_id = ? AND account_id IS NOT NULL AND account_id != ''
             AND status IN ('assigned', 'informed', 'deployed')
             AND is_reply = 0 AND comment_type != 'op_reply'""",
        (post_id,)
    ).fetchall()
    return {r["account_id"] for r in rows}


# ---------------------------------------------------------------------------
# Scoring — comment assignment
# ---------------------------------------------------------------------------

def score_account(account, comment, lookups, batch_picks):
    """Score an (account, comment) pair. Higher = better fit.

    Design principle: ACTIVITY-BASED penalties dominate.
    Karma/age are only tiebreakers — they should never override
    the fact that an account just posted or was recently assigned.
    """
    username = account["username"]
    score = 100

    # =====================================================================
    # HEAVY penalties — activity & load (these decide assignment)
    # Goal: guarantee max participation / round-robin behavior.
    # Each penalty must exceed the max bonus gap between accounts (~53 pts)
    # so that ANY fresh account always beats ANY account with 1+ assignment.
    # =====================================================================

    # --- Subreddit cooldown: -40 per assignment in same sub within ±2 days ---
    comment_day = comment.get("suggested_post_day", 0) or 0
    sub_day = lookups["sub_day"]
    for day_offset in range(-2, 3):
        score -= 40 * sub_day[username].get(comment_day + day_offset, 0)

    # --- Load balancing: -55 per pending assignment globally ---
    score -= 55 * lookups["pending"].get(username, 0)

    # --- Global deployed footprint: -30 per deployed comment ---
    # 2 deployments = -60, guaranteed behind any fresh account
    deployed_count = lookups["deployed"].get(username, 0)
    score -= 30 * deployed_count

    # --- Post ownership load: -55 per post owned ---
    # 1 post owned = guaranteed behind any fresh account
    score -= 55 * lookups["post_ownership"].get(username, 0)

    # --- Batch spread: -55 per pick (flat, no reuse before all accounts used) ---
    picks = batch_picks.get(username, 0)
    score -= 55 * picks

    # =====================================================================
    # LIGHT bonuses — credibility signals (only break ties)
    # =====================================================================

    # --- Karma bonus: +2 per 1000 karma, capped at +10 ---
    # (was +5/1000 capped at +35 — way too dominant)
    total_karma = (account.get("link_karma") or 0) + (account.get("comment_karma") or 0)
    score += min(10, 2 * (total_karma // 1000))

    # --- Low karma penalty: -10 if total < 500 ---
    if total_karma < 500:
        score -= 10

    # --- Account age: only penalize very new, minimal bonus for old ---
    created = account.get("created_utc")
    if created:
        age_days = (time.time() - created) / 86400
        if age_days < 30:
            score -= 15   # Very new — suspicious
        elif age_days < 90:
            score -= 5    # Still building credibility
        # No bonus for old accounts — age should not be an advantage

    # --- Subreddit familiarity: +5 if account has history here ---
    # (was +15 — too strong, made veterans always win)
    if username in lookups["veterans"]:
        score += 5

    # --- Subreddit spread bonus: +2 per distinct sub active in (max +8) ---
    score += min(8, 2 * lookups["sub_spread"].get(username, 0))

    # --- Brand skew: proportional penalty starting at 35% concentration ---
    brand_id = comment.get("brand_id")
    if brand_id and comment.get("mentions_brand"):
        total = lookups["total_mentions"].get(username, 0)
        if total > 0:
            ratio = lookups["brand_mentions"][username].get(brand_id, 0) / total
            if ratio > 0.35:
                score -= int(50 * (ratio - 0.35))

    # --- Tiebreaker: deterministic jitter based on username ---
    # Uses md5 (not hash()) because Python's hash() is randomized per process
    score += int(hashlib.md5(username.encode()).hexdigest(), 16) % 6

    return score


# ---------------------------------------------------------------------------
# Scoring — post ownership
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
    """Score an account for post ownership. Activity penalties dominate."""
    username = account["username"]
    score = 100

    # =====================================================================
    # HEAVY penalties — activity & load
    # =====================================================================

    # --- Subreddit post concentration: -40 per post in this sub ---
    score -= 40 * lookups["sub_posts"].get(username, 0)

    # --- Global post load: -20 per post across all subs ---
    score -= 20 * lookups["global_posts"].get(username, 0)

    # --- Batch spread: progressive ---
    picks = batch_picks.get(username, 0)
    if picks <= 2:
        score -= 25 * picks
    else:
        score -= 25 * 2 + 40 * (picks - 2)

    # =====================================================================
    # LIGHT bonuses — tiebreakers only
    # =====================================================================

    # --- Subreddit activity: +5 if account has comments here ---
    if lookups["sub_comments"].get(username, 0) > 0:
        score += 5

    # --- Subreddit owner bonus: +10 ---
    if username == sub_owner:
        score += 10

    # --- Karma bonus: +2 per 1000, capped at +10 ---
    total_karma = (account.get("link_karma") or 0) + (account.get("comment_karma") or 0)
    score += min(10, 2 * (total_karma // 1000))

    # --- Low karma penalty ---
    if total_karma < 500:
        score -= 10

    # --- Account age: only penalize very new ---
    created = account.get("created_utc")
    if created:
        age_days = (time.time() - created) / 86400
        if age_days < 30:
            score -= 15
        elif age_days < 90:
            score -= 5

    # --- Tiebreaker ---
    score += random.randint(0, 5)

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
      - OP replies → post owner only
      - Top-level non-OP → unique account per post, never the post owner
      - Replies → normal scoring, any account eligible
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
    # 2. Top-level non-OP: unique account per post, never the OP
    # ---------------------------------------------------------------
    op_to_exclude = op_account or post.get("owner_account") or ""
    blocked = set(used_on_post)
    if op_to_exclude:
        blocked.add(op_to_exclude)

    skipped = 0
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

    # ---------------------------------------------------------------
    # 3. Reply comments: normal scoring, any account eligible
    #    (exempt from uniqueness + OP exclusion)
    # ---------------------------------------------------------------
    for comment in reply_comments:
        scores = [(score_account(a, comment, lookups, batch_picks), a) for a in accounts]
        scores.sort(key=lambda x: -x[0])
        best_score, best_account = scores[0]

        if best_score < 0:
            warnings.append(
                f"Reply #{comment['id']}: low confidence score ({best_score}). "
                f"Consider adding more accounts."
            )

        username = best_account["username"]
        db.assign_comment(comment["id"], username)
        batch_picks[username] += 1
        lookups["pending"][username] = lookups["pending"].get(username, 0) + 1

        comment_day = comment.get("suggested_post_day", 0) or 0
        lookups["sub_day"][username][comment_day] += 1

        assignments.append({
            "comment_id": comment["id"],
            "account": username,
            "score": best_score,
            "type": "reply",
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
      - OP reply → post owner only
      - Top-level non-OP → unique account, never the post owner
      - Reply → normal scoring, any account eligible
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
    # Reply → normal scoring, all accounts eligible
    # ---------------------------------------------------------------
    if comment.get("is_reply") and comment.get("parent_comment_id"):
        scores = [(score_account(a, comment, lookups, batch_picks), a) for a in accounts]
        scores.sort(key=lambda x: -x[0])
        best_score, best_account = scores[0]
        username = best_account["username"]
        db.assign_comment(comment_id, username)
        return {"ok": True, "account": username, "score": best_score, "type": "reply"}

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


# ---------------------------------------------------------------------------
# Search comment auto-assignment
# ---------------------------------------------------------------------------

def _get_search_post_toplevel_accounts(db, search_post_id):
    """Return set of account usernames that already have a top-level
    (non-reply) comment assigned/informed/deployed on this search post."""
    rows = db.conn.execute(
        """SELECT DISTINCT account_id FROM search_comments
           WHERE search_post_id = ? AND account_id IS NOT NULL AND account_id != ''
             AND status IN ('assigned', 'informed', 'deployed')
             AND is_reply = 0""",
        (search_post_id,)
    ).fetchall()
    return {r["account_id"] for r in rows}


def auto_assign_single_search_comment(db, comment_id, exclude_accounts=None):
    """Auto-assign a single search comment to the best-scoring account.

    Rules:
      - Non-reply: unique account per search post (no account already used on that post)
      - Reply: normal scoring, any account eligible
    """
    comment = dict(db.conn.execute(
        """SELECT sc.*, sp.subreddit, sp.title as post_title
           FROM search_comments sc
           JOIN search_posts sp ON sc.search_post_id = sp.id
           WHERE sc.id = ?""", (comment_id,)
    ).fetchone() or {})
    if not comment.get("id"):
        return {"error": "Comment not found"}
    if comment.get("account_id") and comment["status"] not in ("draft",):
        return {"error": "Comment already assigned"}

    context = db.get_search_auto_assign_context()
    accounts = context["all_accounts"]
    if exclude_accounts:
        accounts = [a for a in accounts if a["username"] not in set(exclude_accounts)]
    if not accounts:
        return {"error": "No accounts available"}

    lookups = _build_lookups(context)
    batch_picks = defaultdict(int)
    is_reply = comment.get("is_reply", 0)

    if is_reply:
        eligible = accounts
    else:
        blocked = _get_search_post_toplevel_accounts(db, comment["search_post_id"])
        eligible = [a for a in accounts if a["username"] not in blocked]

    if not eligible:
        return {"error": "No eligible account — all accounts already used on this post."}

    scores = [(score_account(a, comment, lookups, batch_picks), a) for a in eligible]
    scores.sort(key=lambda x: -x[0])
    best_score, best_account = scores[0]
    username = best_account["username"]

    db.assign_search_comment(comment_id, username)
    return {"ok": True, "account": username, "score": best_score}


def auto_assign_search_comments(db, exclude_accounts=None):
    """Auto-assign all draft search comments using the same scoring as regular comments.

    Groups comments by search_post_id and enforces uniqueness per post for
    top-level comments. Reply comments are exempt from uniqueness.
    """
    context = db.get_search_auto_assign_context()
    draft_comments = context["draft_comments"]
    if not draft_comments:
        return {"assigned": 0, "skipped": 0, "assignments": [], "warnings": ["No draft search comments"]}

    accounts = context["all_accounts"]
    exclude_set = set(exclude_accounts or [])
    accounts = [a for a in accounts if a["username"] not in exclude_set]
    if not accounts:
        return {"error": "No accounts available after exclusions"}

    lookups = _build_lookups(context)
    batch_picks = defaultdict(int)
    assignments = []
    skipped = 0
    warnings = []

    # Group by search_post_id
    by_post = defaultdict(list)
    for c in draft_comments:
        by_post[c["search_post_id"]].append(c)

    for post_id, comments in by_post.items():
        # Track accounts already assigned to top-level comments on this post
        blocked = _get_search_post_toplevel_accounts(db, post_id)

        for comment in comments:
            is_reply = comment.get("is_reply", 0)

            # Build eligible account list
            if is_reply:
                eligible = accounts  # replies: any account
            else:
                eligible = [a for a in accounts if a["username"] not in blocked]

            if not eligible:
                skipped += 1
                warnings.append(f"No eligible account for comment {comment['id']} (all blocked on post)")
                continue

            # Score each eligible account
            scores = []
            for a in eligible:
                s = score_account(a, comment, lookups, batch_picks)
                scores.append((s, a))
            scores.sort(key=lambda x: -x[0])
            best_score, best_account = scores[0]
            username = best_account["username"]

            # Assign
            db.assign_search_comment(comment["id"], username)
            assignments.append({
                "comment_id": comment["id"],
                "account": username,
                "score": best_score,
                "subreddit": comment.get("subreddit", ""),
            })

            # Update tracking
            batch_picks[username] += 1
            lookups["pending"][username] = lookups["pending"].get(username, 0) + 1
            if not is_reply:
                blocked.add(username)

    return {
        "assigned": len(assignments),
        "skipped": skipped,
        "assignments": assignments,
        "warnings": warnings,
    }
