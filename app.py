#!/usr/bin/env python3
"""Reddit Strategy Bot — Flask Web Dashboard."""

import sys
import os
import csv
import io
import json
import uuid
import threading
from functools import wraps

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, render_template, request, jsonify, Response, redirect, url_for, session
from authlib.integrations.flask_client import OAuth
from config import (
    ANTHROPIC_API_KEY, DB_PATH, DEFAULT_BRAND_MENTION_RATIO,
    SECRET_KEY, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, ALLOWED_EMAILS,
)
from db import Database
from generators.base import ClaudeClient
from generators.subreddit_gen import SubredditGenerator
from generators.post_gen import PostGenerator
from generators.comment_gen import CommentGenerator

app = Flask(__name__)
app.secret_key = SECRET_KEY or os.urandom(32)

# --- Proxy / HTTPS support (Railway, Render, etc.) ---
# Flask needs to know it's behind a reverse proxy serving HTTPS
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config['PREFERRED_URL_SCHEME'] = 'https'

# --- Session security ---
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER"):
    app.config['SESSION_COOKIE_SECURE'] = True

# --- Google OAuth ---
_auth_enabled = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

oauth = OAuth(app)
if _auth_enabled:
    google = oauth.register(
        'google',
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'},
    )

_PUBLIC_PATHS = {'/login', '/auth/google', '/auth/callback', '/auth/logout'}

@app.before_request
def require_login():
    if not _auth_enabled:
        return  # Auth disabled in dev (no Google creds configured)
    if request.path in _PUBLIC_PATHS or request.path.startswith('/static/'):
        return
    if not session.get('user_email'):
        if request.path.startswith('/api/'):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect('/login')

def login_required(f):
    """Kept for explicit use if needed; before_request handles global auth."""
    @wraps(f)
    def decorated(*args, **kwargs):
        return f(*args, **kwargs)
    return decorated

@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response

# ---------------------------------------------------------------------------
# Database helper — main thread DB
# ---------------------------------------------------------------------------

def get_db():
    db = Database(DB_PATH)
    db.connect()
    db.initialize()
    return db

# ---------------------------------------------------------------------------
# Background task system
# ---------------------------------------------------------------------------

tasks = {}  # {task_id: {status, result, error, type}}

def run_task(task_id, func, *args, **kwargs):
    """Run a function in background thread with its own DB connection."""
    try:
        result = func(*args, **kwargs)
        tasks[task_id]["status"] = "complete"
        tasks[task_id]["result"] = result
    except Exception as e:
        tasks[task_id]["status"] = "error"
        tasks[task_id]["error"] = str(e)

def start_task(task_type, func, *args, **kwargs):
    task_id = str(uuid.uuid4())
    tasks[task_id] = {"status": "running", "type": task_type, "result": None, "error": None}
    t = threading.Thread(target=run_task, args=(task_id, func, *args), kwargs=kwargs, daemon=True)
    t.start()
    return task_id

def make_generators():
    """Create fresh DB + generators for a background thread."""
    api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
    db = Database(DB_PATH)
    db.connect()
    db.initialize()
    claude = ClaudeClient(api_key)
    sub_gen = SubredditGenerator(claude)
    post_gen = PostGenerator(claude, db)
    comment_gen = CommentGenerator(claude, db)
    return db, claude, sub_gen, post_gen, comment_gen

# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login")
def login_page():
    if session.get('user_email'):
        return redirect('/')
    error = request.args.get('error')
    return render_template("login.html", error=error)

@app.route("/auth/google")
def auth_google():
    if not _auth_enabled:
        return redirect('/')
    redirect_uri = url_for('auth_callback', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route("/auth/callback")
def auth_callback():
    if not _auth_enabled:
        return redirect('/')
    try:
        token = google.authorize_access_token()
        user_info = token.get('userinfo') or google.userinfo()
        email = user_info.get('email', '').lower()
        if email not in ALLOWED_EMAILS:
            return redirect('/login?error=' + f'{email} is not authorized. Contact your admin.')
        session['user_email'] = email
        session['user_name'] = user_info.get('name', email)
        session.permanent = True
        app.permanent_session_lifetime = __import__('datetime').timedelta(days=7)
        return redirect('/')
    except Exception as e:
        return redirect('/login?error=Authentication failed. Please try again.')

@app.route("/auth/logout")
def auth_logout():
    session.clear()
    return redirect('/login')

# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    return render_template("index.html")

# ---------------------------------------------------------------------------
# API: Dashboard
# ---------------------------------------------------------------------------

@app.route("/api/dashboard")
def api_dashboard():
    db = get_db()
    try:
        subs = db.list_subreddits()
        total_brands = sum(s["brand_count"] for s in subs)
        total_posts = sum(s["post_count"] for s in subs)
        total_comments = sum(s["comment_count"] for s in subs)
        return jsonify({
            "subreddits": len(subs),
            "brands": total_brands,
            "posts": total_posts,
            "comments": total_comments,
        })
    finally:
        db.close()

# ---------------------------------------------------------------------------
# API: Subreddits
# ---------------------------------------------------------------------------

@app.route("/api/subreddits")
def api_list_subreddits():
    db = get_db()
    try:
        return jsonify(db.list_subreddits())
    finally:
        db.close()

@app.route("/api/subreddits/<int:sid>")
def api_get_subreddit(sid):
    db = get_db()
    try:
        sub = db.get_subreddit(sid)
        if not sub:
            return jsonify({"error": "Not found"}), 404
        sub["stats"] = db.get_stats_for_subreddit(sid)
        sub["brands"] = db.list_brands(sid)
        return jsonify(sub)
    finally:
        db.close()

@app.route("/api/subreddits/<int:sid>", methods=["DELETE"])
def api_delete_subreddit(sid):
    db = get_db()
    try:
        sub = db.get_subreddit(sid)
        if not sub:
            return jsonify({"error": "Not found"}), 404
        db.delete_subreddit(sid)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/subreddits/<int:sid>/owner", methods=["PUT"])
def api_set_subreddit_owner(sid):
    db = get_db()
    try:
        data = request.json
        username = data.get("username", "")
        db.set_subreddit_owner(sid, username)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/subreddits", methods=["POST"])
def api_create_subreddit():
    db = get_db()
    try:
        data = request.json
        sid = db.create_subreddit(
            name=data["name"],
            domain=data["domain"],
            description=data.get("description", ""),
            rules=data.get("rules", "[]"),
            sidebar=data.get("sidebar", ""),
            welcome_message=data.get("welcome_message", ""),
        )
        return jsonify({"id": sid})
    finally:
        db.close()

# ---------------------------------------------------------------------------
# API: Brands
# ---------------------------------------------------------------------------

@app.route("/api/subreddits/<int:sid>/brands")
def api_list_brands(sid):
    db = get_db()
    try:
        return jsonify(db.list_brands(sid))
    finally:
        db.close()

@app.route("/api/subreddits/<int:sid>/brands", methods=["POST"])
def api_add_brand(sid):
    db = get_db()
    try:
        data = request.json
        bid = db.add_brand(
            subreddit_id=sid,
            name=data["name"],
            domain_url=data.get("domain_url", ""),
            context=data.get("context", ""),
            keywords=json.dumps(data.get("keywords", [])),
        )
        return jsonify({"id": bid})
    finally:
        db.close()

# ---------------------------------------------------------------------------
# API: Posts
# ---------------------------------------------------------------------------

@app.route("/api/subreddits/<int:sid>/posts")
def api_list_posts(sid):
    db = get_db()
    try:
        brand_id = request.args.get("brand_id", type=int)
        include_filler = request.args.get("include_filler", "true") == "true"
        posts = db.get_posts(sid, brand_id=brand_id, include_filler=include_filler, limit=200)
        # Attach comment count to each post
        for p in posts:
            comments = db.get_comments(p["id"])
            p["comment_count"] = len(comments)
            p["reddit_url"] = db.get_url_for_post(p["id"])
        return jsonify(posts)
    finally:
        db.close()

@app.route("/api/posts/<int:pid>")
def api_get_post(pid):
    db = get_db()
    try:
        post = db.get_post(pid)
        if not post:
            return jsonify({"error": "Not found"}), 404
        post["reddit_url"] = db.get_url_for_post(pid)
        post["comment_count"] = len(db.get_comments(pid))
        return jsonify(post)
    finally:
        db.close()

@app.route("/api/posts/<int:pid>/status", methods=["POST"])
def api_update_post_status(pid):
    db = get_db()
    try:
        data = request.json
        db.update_post_status(pid, data["status"])
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/posts/<int:pid>/publish", methods=["POST"])
def api_publish_post(pid):
    db = get_db()
    try:
        data = request.json
        reddit_url = data.get("reddit_url", "")
        owner_account = data.get("owner_account", "")
        db.update_post_status(pid, "published")
        post = db.get_post(pid)
        if reddit_url and post:
            db.link_url_to_post(pid, reddit_url, post["subreddit_id"])
        if owner_account:
            db.set_post_owner(pid, owner_account)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/posts/<int:pid>", methods=["DELETE"])
def api_delete_post(pid):
    db = get_db()
    try:
        db.delete_post(pid)
        return jsonify({"ok": True})
    finally:
        db.close()

# ---------------------------------------------------------------------------
# API: Comments
# ---------------------------------------------------------------------------

@app.route("/api/posts/<int:pid>/comments")
def api_get_comments(pid):
    db = get_db()
    try:
        tree = db.get_comment_tree(pid)
        return jsonify(tree)
    finally:
        db.close()

@app.route("/api/comments/<int:cid>/mark-ours", methods=["POST"])
def api_mark_comment_ours(cid):
    db = get_db()
    try:
        data = request.json
        db.mark_comment_ours(cid, data.get("is_ours", True))
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/comments/save-organic", methods=["POST"])
def api_save_organic_comment():
    """Save an organic Reddit comment (not generated by us) into the DB and mark as ours."""
    db = get_db()
    try:
        data = request.json
        post_id = data.get("post_id")
        if not post_id:
            return jsonify({"error": "post_id required"}), 400

        post = db.get_post(post_id)
        if not post:
            return jsonify({"error": "Post not found"}), 404

        body = data.get("body", "")
        author = data.get("author", "")
        permalink = data.get("permalink", "")
        reddit_comment_url = data.get("reddit_comment_url", "")
        if permalink and not reddit_comment_url:
            reddit_comment_url = f"https://www.reddit.com{permalink}"

        # Save as a deployed comment (it's already live on Reddit)
        comment_id = db.save_comment(
            post_id=post_id,
            brand_id=post.get("brand_id"),
            body=body,
            persona_id=None,
            structure_id=None,
            is_reply=0,
            parent_comment_id=None,
            mentions_brand=0,
            validation_score=None,
            account_id=author,
            status="deployed",
            suggested_post_day=post.get("suggested_post_day", 0),
            suggested_order=0,
            prompt_version=None,
        )

        # Set is_ours, reddit_comment_url, deployed_at
        from datetime import datetime
        db.mark_comment_ours(comment_id, True)
        if reddit_comment_url:
            db.deploy_comment(comment_id, reddit_comment_url, datetime.utcnow().isoformat())

        # Detect brand keyword matches if brand exists
        brand = db.get_brand(post.get("brand_id")) if post.get("brand_id") else None
        if brand:
            import re
            keywords = json.loads(brand.get("keywords", "[]")) if brand.get("keywords") else []
            matched = [kw for kw in keywords if re.search(r'\b' + re.escape(kw) + r'\b', body, re.IGNORECASE)]
            if matched:
                db.update_matched_keywords(comment_id, json.dumps(matched))
            if any(re.search(r'\b' + re.escape(kw) + r'\b', body, re.IGNORECASE) for kw in keywords):
                db.conn.execute("UPDATE comments SET mentions_brand = 1 WHERE id = ?", (comment_id,))
                db.conn.commit()

        return jsonify({"ok": True, "comment_id": comment_id})
    finally:
        db.close()

@app.route("/api/posts/<int:pid>/live-comments")
def api_live_comments(pid):
    """Fetch live Reddit comments for a published post and cross-reference with our DB."""
    db = get_db()
    try:
        post = db.get_post(pid)
        if not post:
            return jsonify({"error": "Post not found"}), 404

        reddit_url = db.get_url_for_post(pid)
        if not reddit_url:
            return jsonify({"error": "No Reddit URL linked to this post"}), 400

        # Fetch live comments from Reddit
        api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
        claude = ClaudeClient(api_key)
        comment_gen = CommentGenerator(claude, db)
        live_comments, _, _ = comment_gen.fetch_comments(reddit_url, limit=50)

        # Get our DB comments for this post
        our_comments = db.get_comments(pid)

        # Cross-reference: fuzzy match live comments against our DB comments
        for lc in live_comments:
            lc["is_ours"] = False
            lc["our_comment_id"] = None
            live_body = lc["body"].strip().lower()[:200]
            for oc in our_comments:
                our_body = oc["body"].strip().lower()[:200]
                # Check if bodies are similar (first 200 chars match closely)
                if live_body == our_body or (len(live_body) > 50 and live_body[:100] in our_body):
                    lc["is_ours"] = True
                    lc["our_comment_id"] = oc["id"]
                    break

        return jsonify({
            "reddit_url": reddit_url,
            "comments": live_comments,
            "our_comment_count": len(our_comments),
        })
    finally:
        db.close()

@app.route("/api/subreddits/<int:sid>/published-posts")
def api_published_posts(sid):
    db = get_db()
    try:
        return jsonify(db.get_published_posts_with_urls(sid))
    finally:
        db.close()

@app.route("/api/comments/<int:cid>/status", methods=["POST"])
def api_update_comment_status(cid):
    db = get_db()
    try:
        data = request.json
        db.update_comment_status(cid, data["status"])
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/comments/<int:cid>/assign", methods=["POST"])
def api_assign_comment(cid):
    db = get_db()
    try:
        data = request.json
        db.assign_comment(cid, data.get("account_id", ""))
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/comments/<int:cid>/unassign", methods=["POST"])
def api_unassign_comment(cid):
    db = get_db()
    try:
        db.unassign_comment(cid)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/comments/<int:cid>/context")
def api_comment_context(cid):
    """Get comment with its subreddit and brand context for the assignment modal."""
    db = get_db()
    try:
        row = db.conn.execute(
            """SELECT c.id, c.post_id, c.brand_id, p.subreddit_id,
                      s.name as subreddit_name, b.name as brand_name
               FROM comments c
               JOIN posts p ON c.post_id = p.id
               LEFT JOIN subreddits s ON p.subreddit_id = s.id
               LEFT JOIN brands b ON c.brand_id = b.id
               WHERE c.id = ?""", (cid,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        return jsonify(dict(row))
    finally:
        db.close()

@app.route("/api/comments/<int:cid>/deploy", methods=["POST"])
def api_deploy_comment(cid):
    db = get_db()
    try:
        data = request.json
        db.deploy_comment(
            cid,
            data.get("reddit_comment_url", ""),
            data.get("deployed_at"),
        )
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/comments/<int:cid>/mark-deleted", methods=["POST"])
def api_mark_comment_deleted(cid):
    db = get_db()
    try:
        db.mark_comment_deleted(cid)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/subreddits/<int:sid>/all-comments")
def api_all_comments(sid):
    db = get_db()
    try:
        status = request.args.get("status")
        mentions_brand = request.args.get("mentions_brand")
        account_id = request.args.get("account_id")
        mb = None
        if mentions_brand == "1":
            mb = True
        elif mentions_brand == "0":
            mb = False
        comments = db.get_filtered_comments(sid, status=status or None, mentions_brand=mb, account_id=account_id or None)
        return jsonify(comments)
    finally:
        db.close()

@app.route("/api/subreddits/<int:sid>/check-live", methods=["POST"])
def api_check_live(sid):
    import time as _time
    import requests as _requests

    def task():
        db = Database(DB_PATH)
        db.connect()
        db.initialize()
        try:
            deployed = db.get_deployed_comment_urls(sid)
            checked = 0
            live = 0
            dead = 0
            errors = 0
            for item in deployed:
                checked += 1
                url = item["reddit_comment_url"]
                try:
                    # Fetch the comment JSON from Reddit
                    clean = url.split("?")[0].rstrip("/")
                    json_url = f"{clean}.json"
                    resp = _requests.get(json_url, headers={"User-Agent": "RedditStrategyBot/1.0"}, timeout=15)
                    if resp.status_code == 404:
                        db.mark_comment_deleted(item["id"])
                        dead += 1
                    elif resp.status_code == 200:
                        data = resp.json()
                        # Reddit comment URL .json returns a list of listings
                        found_deleted = False
                        if isinstance(data, list) and len(data) > 1:
                            children = data[1].get("data", {}).get("children", [])
                            for child in children:
                                body = child.get("data", {}).get("body", "")
                                if body in ("[deleted]", "[removed]"):
                                    found_deleted = True
                                    break
                        if found_deleted:
                            db.mark_comment_deleted(item["id"])
                            dead += 1
                        else:
                            live += 1
                    else:
                        errors += 1
                except Exception:
                    errors += 1
                _time.sleep(2)
            return {"checked": checked, "live": live, "dead": dead, "errors": errors}
        finally:
            db.close()

    tid = start_task("check-live", task)
    return jsonify({"task_id": tid})

@app.route("/api/subreddits/<int:sid>/backfill-keywords", methods=["POST"])
def api_backfill_keywords(sid):
    def task():
        db = Database(DB_PATH)
        db.connect()
        db.initialize()
        try:
            return db.backfill_matched_keywords(sid)
        finally:
            db.close()

    tid = start_task("backfill-keywords", task)
    return jsonify({"task_id": tid})

@app.route("/api/brands/all")
def api_all_brands():
    db = get_db()
    try:
        return jsonify(db.get_all_brands())
    finally:
        db.close()

@app.route("/api/brands/<int:bid>/deployed-comments")
def api_brand_deployed_comments(bid):
    db = get_db()
    try:
        return jsonify(db.get_deployed_comments_by_brand(brand_id=bid))
    finally:
        db.close()

@app.route("/api/comments/live-stats", methods=["POST"])
def api_comments_live_stats():
    """Fetch live Reddit stats (upvotes, replies) for a list of comment URLs server-side."""
    import time as _time
    import requests as _requests

    data = request.json
    urls = data.get("urls", [])  # list of {id, reddit_comment_url}

    def task():
        results = {}
        for item in urls:
            cid = item.get("id")
            url = item.get("reddit_comment_url", "")
            if not url:
                continue
            try:
                clean = url.split("?")[0].rstrip("/")
                json_url = f"{clean}.json"
                resp = _requests.get(json_url, headers={"User-Agent": "RedditStrategyBot/1.0"}, timeout=15)
                if resp.status_code == 200:
                    rdata = resp.json()
                    if isinstance(rdata, list) and len(rdata) > 1:
                        children = rdata[1].get("data", {}).get("children", [])
                        for child in children:
                            if child.get("kind") == "t1":
                                cd = child.get("data", {})
                                replies_obj = cd.get("replies", "")
                                num_replies = 0
                                if isinstance(replies_obj, dict):
                                    num_replies = len(replies_obj.get("data", {}).get("children", []))
                                results[str(cid)] = {
                                    "score": cd.get("score", 0),
                                    "author": cd.get("author", ""),
                                    "num_replies": num_replies,
                                    "permalink": cd.get("permalink", ""),
                                    "created_utc": cd.get("created_utc", 0),
                                }
                                break
            except Exception:
                pass
            _time.sleep(2)
        return results

    tid = start_task("live-stats", task)
    return jsonify({"task_id": tid})

@app.route("/api/comments/<int:cid>", methods=["DELETE"])
def api_delete_comment(cid):
    db = get_db()
    try:
        db.delete_comment(cid)
        return jsonify({"ok": True})
    finally:
        db.close()

# ---------------------------------------------------------------------------
# API: Analytics
# ---------------------------------------------------------------------------

@app.route("/api/subreddits/<int:sid>/storylines")
def api_storylines(sid):
    db = get_db()
    try:
        return jsonify(db.get_storyline_distribution(sid))
    finally:
        db.close()

@app.route("/api/subreddits/<int:sid>/schedule")
def api_schedule(sid):
    db = get_db()
    try:
        schedule = db.get_schedule_status(sid)
        # Convert int keys to strings for JSON
        return jsonify({str(k): v for k, v in schedule.items()})
    finally:
        db.close()

@app.route("/api/brands/<name>/performance")
def api_brand_performance(name):
    db = get_db()
    try:
        mention = db.get_brand_mention_ratio(brand_name=name)
        personas = db.get_persona_distribution(brand_name=name)
        total_posts = len(db.get_all_post_titles_for_brand(name))
        return jsonify({
            "brand_name": name,
            "total_posts": total_posts,
            "mention_ratio": mention,
            "personas": personas,
        })
    finally:
        db.close()

@app.route("/api/subreddits/<int:sid>/health")
def api_content_health(sid):
    db = get_db()
    try:
        sub = db.get_subreddit(sid)
        if not sub:
            return jsonify({"error": "Not found"}), 404

        all_posts = db.get_posts(sid)
        all_comments = []
        for p in all_posts:
            all_comments.extend(db.get_comments(p["id"]))

        if not all_comments:
            return jsonify({
                "total_comments": 0,
                "duplicate_openings": [],
                "max_consecutive_persona": 0,
                "low_quality": [],
                "avg_length": 0,
                "brand_placement": {"early": 0, "mid": 0, "late": 0},
            })

        # Duplicate openings
        from collections import Counter
        openings = [" ".join(c["body"].split()[:5]).lower() for c in all_comments]
        opening_counts = Counter(openings)
        dupes = [{"text": o, "count": cnt} for o, cnt in opening_counts.items() if cnt > 1]

        # Consecutive persona runs
        personas_seq = [c.get("persona_id", "") for c in all_comments if c.get("persona_id")]
        max_consecutive = 1
        current_run = 1
        for i in range(1, len(personas_seq)):
            if personas_seq[i] == personas_seq[i - 1]:
                current_run += 1
                max_consecutive = max(max_consecutive, current_run)
            else:
                current_run = 1

        # Low quality
        low_quality = []
        for c in all_comments:
            if c.get("validation_score") and c["validation_score"] < 6:
                low_quality.append({
                    "id": c["id"],
                    "score": c["validation_score"],
                    "preview": c["body"][:80],
                })

        # Average length
        lengths = [len(c["body"].split()) for c in all_comments]
        avg_len = sum(lengths) / len(lengths) if lengths else 0

        # Brand placement
        brand_comments = [c for c in all_comments if c.get("mentions_brand")]
        brands = db.list_brands(sid)
        placements = {"early": 0, "mid": 0, "late": 0}
        for c in brand_comments:
            body = c["body"].lower()
            total_len = len(body)
            for b in brands:
                pos = body.find(b["name"].lower())
                if pos >= 0:
                    rel = pos / total_len if total_len > 0 else 0.5
                    if rel < 0.33:
                        placements["early"] += 1
                    elif rel < 0.66:
                        placements["mid"] += 1
                    else:
                        placements["late"] += 1
                    break

        return jsonify({
            "total_comments": len(all_comments),
            "duplicate_openings": dupes[:5],
            "max_consecutive_persona": max_consecutive,
            "low_quality": low_quality[:5],
            "avg_length": round(avg_len),
            "brand_placement": placements,
        })
    finally:
        db.close()

# ---------------------------------------------------------------------------
# API: Brand auto-analysis from website
# ---------------------------------------------------------------------------

@app.route("/api/analyze-brand", methods=["POST"])
def api_analyze_brand():
    data = request.json

    def task():
        db, claude, _, _, comment_gen = make_generators()
        try:
            domain = data.get("domain_url", "")
            if not domain:
                raise ValueError("domain_url is required")
            result = comment_gen.extract_brand_info(domain)
            if not result:
                raise ValueError(f"Could not extract brand info from {domain}")
            return result
        finally:
            db.close()

    tid = start_task("analyze-brand", task)
    return jsonify({"task_id": tid})

# ---------------------------------------------------------------------------
# API: Generation (background tasks)
# ---------------------------------------------------------------------------

@app.route("/api/generate/subreddit-names", methods=["POST"])
def api_gen_subreddit_names():
    data = request.json

    def task():
        db, claude, sub_gen, _, _ = make_generators()
        try:
            names = data.get("brand_names", [])
            contexts = data.get("brand_contexts", [])
            count = data.get("count", 8)
            return sub_gen.generate_names(names, contexts, count)
        finally:
            db.close()

    tid = start_task("subreddit-names", task)
    return jsonify({"task_id": tid})

@app.route("/api/generate/subreddit-info", methods=["POST"])
def api_gen_subreddit_info():
    data = request.json

    def task():
        db, claude, sub_gen, _, _ = make_generators()
        try:
            return sub_gen.generate_subreddit_info(data["name"], data["domain"])
        finally:
            db.close()

    tid = start_task("subreddit-info", task)
    return jsonify({"task_id": tid})

@app.route("/api/generate/welcome-post", methods=["POST"])
def api_gen_welcome_post():
    data = request.json

    def task():
        db, claude, _, post_gen, _ = make_generators()
        try:
            sub = db.get_subreddit(data["subreddit_id"])
            if not sub:
                raise ValueError("Subreddit not found")
            post = post_gen.generate_welcome_post(sub)
            if not post:
                raise ValueError("Failed to generate welcome post")
            return {"id": post["id"], "title": post["title"]}
        finally:
            db.close()

    tid = start_task("welcome-post", task)
    return jsonify({"task_id": tid})

@app.route("/api/generate/posts", methods=["POST"])
def api_gen_posts():
    data = request.json

    def task():
        db, claude, _, post_gen, _ = make_generators()
        try:
            sub = db.get_subreddit(data["subreddit_id"])
            brand = db.get_brand(data["brand_id"])
            if not sub or not brand:
                raise ValueError("Subreddit or brand not found")
            posts = post_gen.generate_posts(sub, brand, data.get("count", 3))
            return [{"id": p["id"], "title": p["title"], "storyline": p.get("storyline", "")} for p in posts]
        finally:
            db.close()

    tid = start_task("posts", task)
    return jsonify({"task_id": tid})

@app.route("/api/generate/filler-posts", methods=["POST"])
def api_gen_filler_posts():
    data = request.json

    def task():
        db, claude, _, post_gen, _ = make_generators()
        try:
            sub = db.get_subreddit(data["subreddit_id"])
            if not sub:
                raise ValueError("Subreddit not found")
            posts = post_gen.generate_filler_posts(sub, data.get("count", 3))
            return [{"id": p["id"], "title": p["title"]} for p in posts]
        finally:
            db.close()

    tid = start_task("filler-posts", task)
    return jsonify({"task_id": tid})

@app.route("/api/generate/comments", methods=["POST"])
def api_gen_comments():
    data = request.json

    def task():
        db, claude, _, _, comment_gen = make_generators()
        try:
            post = db.get_post(data["post_id"])
            brand = db.get_brand(data["brand_id"])
            if not post or not brand:
                raise ValueError("Post or brand not found")
            ratio = data.get("brand_mention_ratio", DEFAULT_BRAND_MENTION_RATIO)
            comments = comment_gen.generate_comment_tree(
                post, brand, data.get("count", 5),
                brand_mention_ratio=ratio,
                post_day_offset=post.get("suggested_post_day", 0),
            )
            return [{"id": c["id"], "body": c["body"][:100]} for c in comments]
        finally:
            db.close()

    tid = start_task("comments", task)
    return jsonify({"task_id": tid})

@app.route("/api/generate/hq-comment", methods=["POST"])
def api_gen_hq_comment():
    data = request.json

    def task():
        db, claude, _, _, comment_gen = make_generators()
        try:
            post = db.get_post(data["post_id"])
            brand = db.get_brand(data["brand_id"])
            if not post or not brand:
                raise ValueError("Post or brand not found")
            ratio = data.get("brand_mention_ratio", 0.15)
            comments = comment_gen.generate_hq_comment(
                post, brand, brand_mention_ratio=ratio,
                post_day_offset=post.get("suggested_post_day", 0),
            )
            return [{"id": c["id"], "body": c["body"][:100]} for c in comments]
        finally:
            db.close()

    tid = start_task("hq-comment", task)
    return jsonify({"task_id": tid})

@app.route("/api/generate/live-comments", methods=["POST"])
def api_gen_live_comments():
    data = request.json

    def task():
        db, claude, _, _, comment_gen = make_generators()
        try:
            brand = db.get_brand(data["brand_id"])
            if not brand:
                raise ValueError("Brand not found")
            ratio = data.get("brand_mention_ratio", DEFAULT_BRAND_MENTION_RATIO)
            comments = comment_gen.generate_for_existing_post(
                data["reddit_url"],
                data["subreddit_id"],
                brand,
                data.get("count", 5),
                brand_mention_ratio=ratio,
            )
            return [{"id": c["id"], "body": c["body"][:100]} for c in comments]
        finally:
            db.close()

    tid = start_task("live-comments", task)
    return jsonify({"task_id": tid})

# ---------------------------------------------------------------------------
# API: Task polling
# ---------------------------------------------------------------------------

@app.route("/api/tasks/<task_id>")
def api_task_status(task_id):
    t = tasks.get(task_id)
    if not t:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(t)

# ---------------------------------------------------------------------------
# API: Export
# ---------------------------------------------------------------------------

@app.route("/api/export/subreddit/<int:sid>")
def api_export_subreddit(sid):
    db = get_db()
    try:
        sub = db.get_subreddit(sid)
        if not sub:
            return jsonify({"error": "Not found"}), 404

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "type", "post_id", "post_title", "post_body", "storyline",
            "ai_query_score", "is_filler", "status", "day",
            "comment_id", "comment_body", "persona", "structure",
            "is_reply", "parent_comment_id", "mentions_brand",
            "comment_status", "comment_day", "image_prompt"
        ])

        posts = db.get_posts(sid, include_filler=True)
        for post in posts:
            writer.writerow([
                "POST", post["id"], post["title"], post["body"],
                post["storyline"], post["ai_query_score"],
                post["is_filler"], post["status"], post["suggested_post_day"],
                "", "", "", "", "", "", "", "", "", post.get("image_prompt", "")
            ])
            for c in db.get_comments(post["id"]):
                writer.writerow([
                    "COMMENT", post["id"], post["title"], "",
                    "", "", "", "", "",
                    c["id"], c["body"], c.get("persona_id", ""),
                    c.get("structure_id", ""), c["is_reply"],
                    c.get("parent_comment_id", ""), c["mentions_brand"],
                    c["status"], c["suggested_post_day"], ""
                ])

        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={sub['name']}_export.csv"},
        )
    finally:
        db.close()

@app.route("/api/export/schedule/<int:sid>")
def api_export_schedule(sid):
    db = get_db()
    try:
        sub = db.get_subreddit(sid)
        if not sub:
            return jsonify({"error": "Not found"}), 404

        schedule = db.get_schedule_status(sid)
        output = io.StringIO()
        writer = csv.writer(output)
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

        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={sub['name']}_schedule.csv"},
        )
    finally:
        db.close()

@app.route("/api/export/comments/<int:sid>")
def api_export_comments(sid):
    db = get_db()
    try:
        sub = db.get_subreddit(sid)
        if not sub:
            return jsonify({"error": "Not found"}), 404

        status = request.args.get("status")
        mentions_brand = request.args.get("mentions_brand")
        account_id = request.args.get("account_id")
        mb = None
        if mentions_brand == "1":
            mb = True
        elif mentions_brand == "0":
            mb = False
        comments = db.get_filtered_comments(sid, status=status or None, mentions_brand=mb, account_id=account_id or None)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "comment_id", "post_title", "post_link", "comment_body",
            "comment_link", "deployed_at", "status", "account_id",
            "mentions_brand", "matched_keywords", "persona_id",
            "is_reply", "day"
        ])

        for c in comments:
            kw = c.get("matched_keywords", "")
            try:
                kw_list = json.loads(kw) if kw else []
                kw = ", ".join(kw_list)
            except (json.JSONDecodeError, TypeError):
                pass
            writer.writerow([
                c["id"],
                c.get("post_title", ""),
                c.get("post_reddit_url", ""),
                c["body"],
                c.get("reddit_comment_url", ""),
                c.get("deployed_at", ""),
                c["status"],
                c.get("account_id", ""),
                "Yes" if c.get("mentions_brand") else "No",
                kw,
                c.get("persona_id", ""),
                "Reply" if c.get("is_reply") else "Top-level",
                c.get("suggested_post_day", ""),
            ])

        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={sub['name']}_comments.csv"},
        )
    finally:
        db.close()

# ---------------------------------------------------------------------------
# Brand Analytics
# ---------------------------------------------------------------------------

@app.route("/api/brands/unique")
def api_unique_brands():
    db = get_db()
    try:
        return jsonify(db.get_unique_brand_names())
    finally:
        db.close()

@app.route("/api/brands/<name>/full-analytics")
def api_brand_full_analytics(name):
    """Comprehensive brand analytics with date filtering."""
    db = get_db()
    try:
        date_from = request.args.get("date_from") or None
        date_to = request.args.get("date_to") or None

        overview = db.get_brand_overview_stats(name, date_from, date_to)
        subreddit_stats = db.get_brand_subreddit_stats(name, date_from, date_to)
        comments = db.get_brand_comments_with_details(name, date_from, date_to)

        return jsonify({
            "brand_name": name,
            "overview": overview,
            "subreddit_stats": subreddit_stats,
            "comments": comments,
        })
    finally:
        db.close()

@app.route("/api/export/brand/<name>")
def api_export_brand(name):
    """Export brand comments as CSV with optional date filtering."""
    db = get_db()
    try:
        date_from = request.args.get("date_from") or None
        date_to = request.args.get("date_to") or None
        status_filter = request.args.get("status") or None
        mentions_filter = request.args.get("mentions_brand")

        comments = db.get_brand_comments_with_details(name, date_from, date_to)

        # Apply additional filters
        if status_filter:
            comments = [c for c in comments if c["status"] == status_filter]
        if mentions_filter == "1":
            comments = [c for c in comments if c.get("mentions_brand")]
        elif mentions_filter == "0":
            comments = [c for c in comments if not c.get("mentions_brand")]

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "comment_id", "subreddit", "post_title", "post_link",
            "comment_body", "comment_link", "status", "deployed_at",
            "account_id", "is_ours", "mentions_brand", "matched_keywords",
            "persona_id", "is_reply", "created_at",
        ])

        for c in comments:
            kw = c.get("matched_keywords", "")
            try:
                kw_list = json.loads(kw) if kw else []
                kw = ", ".join(kw_list)
            except (json.JSONDecodeError, TypeError):
                pass
            writer.writerow([
                c["id"],
                c.get("subreddit_name", ""),
                c.get("post_title", ""),
                c.get("post_reddit_url", ""),
                c["body"],
                c.get("reddit_comment_url", ""),
                c["status"],
                c.get("deployed_at", ""),
                c.get("account_id", ""),
                "Yes" if c.get("is_ours") else "No",
                "Yes" if c.get("mentions_brand") else "No",
                kw,
                c.get("persona_id", ""),
                "Reply" if c.get("is_reply") else "Top-level",
                c.get("created_at", ""),
            ])

        output.seek(0)
        safe_name = name.replace(" ", "_").lower()
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={safe_name}_brand_comments.csv"},
        )
    finally:
        db.close()

# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def _fetch_reddit_user_data(username):
    """Fetch karma and account age from Reddit user API. Returns dict or None."""
    import requests as _requests
    try:
        resp = _requests.get(
            f"https://www.reddit.com/user/{username}/about.json",
            headers={"User-Agent": "RedditStrategyBot/1.0"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            return {
                "link_karma": data.get("link_karma", 0),
                "comment_karma": data.get("comment_karma", 0),
                "created_utc": data.get("created_utc"),
            }
    except Exception:
        pass
    return None

@app.route("/api/accounts")
def api_list_accounts():
    db = get_db()
    try:
        min_karma = request.args.get("min_karma", type=int)
        min_age_days = request.args.get("min_age_days", type=int)
        reference = request.args.get("reference")
        accounts = db.list_accounts(min_karma=min_karma, min_age_days=min_age_days, reference_search=reference)

        # Enrich with assignment counts
        counts = {}
        rows = db.conn.execute(
            """SELECT account_id, COUNT(*) as cnt FROM comments
               WHERE account_id IS NOT NULL AND account_id != ''
               GROUP BY account_id"""
        ).fetchall()
        for r in rows:
            counts[r["account_id"]] = r["cnt"]
        for a in accounts:
            a["assignment_count"] = counts.get(a["username"], 0)

        return jsonify(accounts)
    finally:
        db.close()

@app.route("/api/accounts", methods=["POST"])
def api_create_account():
    db = get_db()
    try:
        data = request.json
        username = (data.get("username") or "").strip().removeprefix("u/").removeprefix("/").strip()
        if not username:
            return jsonify({"error": "Username required"}), 400

        reference = data.get("reference", "").strip()

        # Check if already exists
        existing = db.get_account(username)
        if existing:
            return jsonify({"error": f"Account '{username}' already exists"}), 409

        # Create account
        db.create_account(username, reference=reference)

        # Fetch Reddit data synchronously (single call)
        reddit_data = _fetch_reddit_user_data(username)
        if reddit_data:
            db.update_account_reddit_data(
                username,
                reddit_data["link_karma"],
                reddit_data["comment_karma"],
                reddit_data["created_utc"],
            )

        account = db.get_account(username)
        return jsonify(account)
    finally:
        db.close()

@app.route("/api/accounts/<username>", methods=["PUT"])
def api_update_account(username):
    db = get_db()
    try:
        data = request.json
        db.update_account_reference(username, data.get("reference", ""))
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/accounts/<username>", methods=["DELETE"])
def api_delete_account(username):
    db = get_db()
    try:
        db.delete_account(username)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/accounts/<username>/refresh", methods=["POST"])
def api_refresh_account(username):
    import time as _time

    def task():
        db2 = Database(DB_PATH)
        db2.connect()
        db2.initialize()
        try:
            reddit_data = _fetch_reddit_user_data(username)
            if reddit_data:
                db2.update_account_reddit_data(
                    username,
                    reddit_data["link_karma"],
                    reddit_data["comment_karma"],
                    reddit_data["created_utc"],
                )
                return {"ok": True, **reddit_data}
            else:
                raise Exception(f"Could not fetch data for u/{username}")
        finally:
            db2.close()

    tid = start_task("refresh-account", task)
    return jsonify({"task_id": tid})

def _parse_accounts_csv(content):
    """Parse CSV content and return (parsed_rows, error_msg). Deduplicates within batch."""
    reader = csv.DictReader(io.StringIO(content))
    fieldnames = reader.fieldnames or []
    header_map = {h.strip().lower(): h for h in fieldnames}
    username_col = header_map.get("username")
    ref_col = header_map.get("ref") or header_map.get("reference")

    if not username_col:
        return None, "CSV must have a 'Username' column"

    parsed_rows = []
    seen = set()
    for row in reader:
        uname = (row.get(username_col) or "").strip().removeprefix("u/").removeprefix("/").strip()
        ref = (row.get(ref_col) or "").strip() if ref_col else ""
        if uname and uname.lower() not in seen:
            seen.add(uname.lower())
            parsed_rows.append((uname, ref))

    if not parsed_rows:
        return None, "No valid rows found"

    return parsed_rows, None

def _run_accounts_import_task(parsed_rows):
    """Create and start a background task to import accounts with Reddit data fetching."""
    import time as _time

    def task():
        db2 = Database(DB_PATH)
        db2.connect()
        db2.initialize()
        try:
            imported = 0
            skipped = 0
            errors = 0
            error_usernames = []
            for username, ref in parsed_rows:
                existing = db2.get_account(username)
                if existing:
                    # Update reference if provided and currently empty
                    if ref and not existing.get("reference"):
                        db2.update_account_reference(username, ref)
                    skipped += 1
                    continue
                db2.create_account(username, reference=ref)
                reddit_data = _fetch_reddit_user_data(username)
                if reddit_data:
                    db2.update_account_reddit_data(
                        username,
                        reddit_data["link_karma"],
                        reddit_data["comment_karma"],
                        reddit_data["created_utc"],
                    )
                    imported += 1
                else:
                    errors += 1
                    error_usernames.append(username)
                _time.sleep(2)
            return {"imported": imported, "skipped": skipped, "errors": errors, "error_usernames": error_usernames}
        finally:
            db2.close()

    return start_task("import-accounts", task)

@app.route("/api/accounts/import-csv", methods=["POST"])
def api_import_accounts_csv():
    """Mass import accounts from CSV file or Google Sheets URL."""
    import requests as _requests

    # Check if this is a Google Sheets URL import (JSON body) or file upload
    if request.content_type and "json" in request.content_type:
        data = request.json
        url = (data.get("url") or "").strip()
        if not url:
            return jsonify({"error": "URL required"}), 400

        # Convert Google Sheets URL to CSV export
        import re
        sheet_match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
        if not sheet_match:
            return jsonify({"error": "Invalid Google Sheets URL. Expected: https://docs.google.com/spreadsheets/d/..."}), 400

        sheet_id = sheet_match.group(1)
        # Extract gid (sheet tab) if present
        gid_match = re.search(r"[#&?]gid=(\d+)", url)
        gid = gid_match.group(1) if gid_match else "0"
        csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

        try:
            resp = _requests.get(csv_url, timeout=30)
            resp.raise_for_status()
            content = resp.text
        except Exception as e:
            return jsonify({"error": f"Could not fetch Google Sheet. Make sure it's publicly accessible. ({str(e)})"}), 400
    else:
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "No file uploaded"}), 400
        content = f.read().decode("utf-8")

    parsed_rows, error = _parse_accounts_csv(content)
    if error:
        return jsonify({"error": error}), 400

    tid = _run_accounts_import_task(parsed_rows)
    return jsonify({"task_id": tid})

@app.route("/api/accounts/assignment-status")
def api_accounts_assignment_status():
    db = get_db()
    try:
        return jsonify(db.get_accounts_with_assignment_counts())
    finally:
        db.close()

@app.route("/api/accounts/<username>/comments")
def api_account_comments(username):
    db = get_db()
    try:
        return jsonify(db.get_comments_for_account(username))
    finally:
        db.close()

@app.route("/api/posts/<int:pid>/owner", methods=["PUT"])
def api_set_post_owner(pid):
    db = get_db()
    try:
        data = request.json
        username = data.get("username", "")
        db.set_post_owner(pid, username)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/accounts/filtered-usernames")
def api_filtered_usernames():
    db = get_db()
    try:
        subreddit_id = request.args.get("subreddit_id", type=int)
        brand_id = request.args.get("brand_id", type=int)
        post_id = request.args.get("post_id", type=int)
        usernames = db.get_accounts_for_filters(subreddit_id, brand_id, post_id)
        return jsonify(usernames)
    finally:
        db.close()

# ---------------------------------------------------------------------------
# Startup logging (runs under both Gunicorn and direct python)
# ---------------------------------------------------------------------------

print("=" * 50)
print("Reddit Strategy Bot — Starting up")
print(f"  Auth enabled: {_auth_enabled}")
print(f"  GOOGLE_CLIENT_ID set: {bool(GOOGLE_CLIENT_ID)}")
print(f"  GOOGLE_CLIENT_SECRET set: {bool(GOOGLE_CLIENT_SECRET)}")
print(f"  ALLOWED_EMAILS: {ALLOWED_EMAILS or '(none)'}")
print(f"  SECRET_KEY set: {bool(SECRET_KEY)}")
print("=" * 50)

# Ensure DB exists
db = get_db()
subs = db.list_subreddits()
total_posts = sum(s["post_count"] for s in subs)
total_comments = sum(s["comment_count"] for s in subs)
print(f"Database: {DB_PATH}")
print(f"  {len(subs)} subreddits | {total_posts} posts | {total_comments} comments")
db.close()

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    print(f"\nStarting web dashboard at http://localhost:{port}")
    app.run(debug=debug, port=port)
