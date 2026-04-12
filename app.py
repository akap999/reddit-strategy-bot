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
    REDDIT_PROXY_URL, REDDIT_USER_AGENT,
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

_db_initialized = False
_karma_backfill_started = False

def _maybe_kick_off_karma_backfill(db):
    """One-time-ever background refresh of accounts that look like silent-failure
    victims (low karma + no recorded error). Runs exactly once per DB because it
    is guarded by the app_meta flag `karma_backfill_done`."""
    global _karma_backfill_started
    if _karma_backfill_started:
        return
    try:
        if db.meta_get("karma_backfill_done") == "1":
            _karma_backfill_started = True
            return
    except Exception:
        return
    _karma_backfill_started = True

    def task():
        import time as _time
        db2 = Database(DB_PATH)
        db2.connect()
        db2.initialize()
        try:
            rows = db2.conn.execute(
                """SELECT username FROM accounts
                   WHERE (COALESCE(link_karma,0) + COALESCE(comment_karma,0)) < 10
                     AND (last_refresh_error IS NULL OR last_refresh_error = '')"""
            ).fetchall()
            refreshed = 0
            failed = 0
            for r in rows:
                uname = r["username"]
                data = _fetch_reddit_user_data(uname)
                if data and "_error" not in data:
                    db2.update_account_reddit_data(
                        uname, data["link_karma"], data["comment_karma"], data["created_utc"]
                    )
                    refreshed += 1
                else:
                    db2.record_refresh_failure(uname, (data or {}).get("_error", "Unknown"))
                    failed += 1
                _time.sleep(1.5)
            db2.meta_set("karma_backfill_done", "1")
            return {"ok": True, "refreshed": refreshed, "failed": failed, "total": len(rows)}
        finally:
            db2.close()

    try:
        start_task("karma-backfill-once", task)
    except Exception as e:
        print(f"[karma-backfill] failed to enqueue: {e}", flush=True)

def get_db():
    global _db_initialized
    db = Database(DB_PATH)
    db.connect()
    if not _db_initialized:
        db.initialize()
        _db_initialized = True
        try:
            _maybe_kick_off_karma_backfill(db)
        except Exception as e:
            print(f"[karma-backfill] init error: {e}", flush=True)
    return db

# ---------------------------------------------------------------------------
# Reddit proxy helper
# ---------------------------------------------------------------------------

def _normalize_reddit_comment_url(url):
    """Normalize a Reddit comment URL to a path suitable for .json fetch.
    Returns the path (e.g. /r/sub/comments/id/.../cid.json) or None if unrecognizable."""
    import re
    if not url:
        return None
    clean = url.split("?")[0].rstrip("/")
    # Strip any reddit domain variant (www, old, new, np, m, etc.)
    path = re.sub(r'^https?://(?:www\.|old\.|new\.|np\.|m\.)?reddit\.com', '', clean)
    # If the path still starts with http, it's an unrecognized domain (e.g. redd.it)
    if path.startswith('http://') or path.startswith('https://'):
        return None
    # Reject share URLs (/s/...) — they return HTML, not JSON
    if path.startswith('/s/'):
        return None
    # Ensure path starts with /
    if not path.startswith('/'):
        return None
    return path + ".json"

def _reddit_get(path, timeout=15, max_retries=3):
    """GET a Reddit API path, routing through Cloudflare proxy if configured.
    path should start with / e.g. /user/spez/about.json
    Retries on transient errors (403, 429, timeouts) with exponential backoff.
    Also retries when proxy returns HTML instead of JSON.
    Falls back to old.reddit.com directly when proxy consistently returns HTML.
    """
    import requests as _requests
    import time as _time
    proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
    base = proxy.rstrip("/") if proxy else "https://old.reddit.com"
    url = f"{base}{path}"
    ua = REDDIT_USER_AGENT
    headers = {"User-Agent": ua, "Accept": "application/json"}
    last_exc = None
    last_resp = None
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                wait = min(2 ** attempt * 2, 15)
                print(f"[REDDIT_GET] Retry {attempt}/{max_retries-1} in {wait}s for {path}", flush=True)
                _time.sleep(wait)
            print(f"[REDDIT_GET] {url} (proxy={'yes' if proxy else 'no'}, attempt={attempt+1})", flush=True)
            resp = _requests.get(url, headers=headers, timeout=timeout)
            # Retry on transient errors
            if resp.status_code in (429, 403, 500, 502, 503, 504) and attempt < max_retries - 1:
                print(f"[REDDIT_GET] Got {resp.status_code}, will retry", flush=True)
                continue
            # Retry if we got HTML instead of JSON (proxy not updated yet)
            if resp.status_code == 200 and resp.text.lstrip()[:1] == "<" and attempt < max_retries - 1:
                print(f"[REDDIT_GET] Got HTML instead of JSON (Content-Type: {resp.headers.get('Content-Type','')}), will retry", flush=True)
                continue
            last_resp = resp
            break
        except (_requests.exceptions.Timeout, _requests.exceptions.ConnectionError) as e:
            last_exc = e
            print(f"[REDDIT_GET] {type(e).__name__} on attempt {attempt+1}: {e}", flush=True)
            if attempt >= max_retries - 1:
                last_resp = None
                break

    # If proxy returned HTML after all retries, try old.reddit.com directly as fallback.
    # old.reddit.com has much less aggressive bot-blocking than www.reddit.com and often
    # returns JSON even from cloud IPs where www.reddit.com would return HTML/403.
    if proxy and last_resp and last_resp.status_code == 200 and last_resp.text.lstrip()[:1] == "<":
        fallback_url = f"https://old.reddit.com{path}"
        print(f"[REDDIT_GET] Proxy returned HTML after retries, trying old.reddit.com fallback: {fallback_url}", flush=True)
        try:
            fb_resp = _requests.get(fallback_url, headers=headers, timeout=timeout)
            if fb_resp.status_code == 200 and fb_resp.text.lstrip()[:1] != "<":
                print(f"[REDDIT_GET] old.reddit.com fallback succeeded (JSON)", flush=True)
                return fb_resp
            else:
                print(f"[REDDIT_GET] old.reddit.com fallback failed (status={fb_resp.status_code}, html={fb_resp.text.lstrip()[:1] == '<'})", flush=True)
        except Exception as fb_err:
            print(f"[REDDIT_GET] old.reddit.com fallback error: {fb_err}", flush=True)

    if last_resp:
        return last_resp
    if last_exc:
        raise last_exc
    raise RuntimeError(f"_reddit_get: no response for {path}")

# ---------------------------------------------------------------------------
# Background task system
# ---------------------------------------------------------------------------

@app.route("/api/debug/reddit-proxy")
def api_debug_reddit_proxy():
    """Test what the proxy returns for a Reddit .json URL — for debugging live checker."""
    import requests as _requests
    test_path = request.args.get("path") or "/r/test/comments/abc.json"
    proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
    ua = REDDIT_USER_AGENT
    results = {}
    # Test proxy
    if proxy:
        try:
            url = f"{proxy.rstrip('/')}{test_path}"
            resp = _requests.get(url, headers={"User-Agent": ua, "Accept": "application/json"}, timeout=15)
            results["proxy"] = {
                "url": url, "status": resp.status_code,
                "content_type": resp.headers.get("Content-Type", ""),
                "body_preview": resp.text[:500],
                "is_json": not resp.text.lstrip().startswith("<"),
            }
        except Exception as e:
            results["proxy"] = {"error": str(e)}
    else:
        results["proxy"] = {"error": "REDDIT_PROXY_URL not configured"}
    # Test direct
    try:
        url = f"https://www.reddit.com{test_path}"
        resp = _requests.get(url, headers={"User-Agent": ua, "Accept": "application/json"}, timeout=15)
        results["direct"] = {
            "url": url, "status": resp.status_code,
            "content_type": resp.headers.get("Content-Type", ""),
            "body_preview": resp.text[:500],
            "is_json": not resp.text.lstrip().startswith("<"),
        }
    except Exception as e:
        results["direct"] = {"error": str(e)}
    return jsonify(results)


def run_task(task_id, func, *args, **kwargs):
    """Run a function in background thread, storing result in DB."""
    try:
        result = func(*args, **kwargs)
        task_db = Database(DB_PATH)
        task_db.connect()
        task_db.update_task(task_id, "complete", result=result)
        task_db.close()
    except Exception as e:
        print(f"[TASK ERROR] {task_id}: {e}", flush=True)
        try:
            task_db = Database(DB_PATH)
            task_db.connect()
            task_db.update_task(task_id, "error", error=str(e))
            task_db.close()
        except Exception as e2:
            print(f"[TASK DB ERROR] {task_id}: {e2}", flush=True)

_task_threads = {}  # task_id -> threading.Thread

def start_task(task_type, func, *args, **kwargs):
    task_id = str(uuid.uuid4())
    db = get_db()
    db.create_task(task_id, task_type)
    db.close()
    t = threading.Thread(target=run_task, args=(task_id, func, *args), kwargs=kwargs, daemon=True)
    _task_threads[task_id] = t
    t.start()
    return task_id

def make_generators():
    """Create fresh DB + generators for a background thread."""
    api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
    reddit_base = REDDIT_PROXY_URL.rstrip("/") if REDDIT_PROXY_URL else None
    db = Database(DB_PATH)
    db.connect()
    db.initialize()
    claude = ClaudeClient(api_key)
    sub_gen = SubredditGenerator(claude, reddit_base=reddit_base)
    post_gen = PostGenerator(claude, db)
    comment_gen = CommentGenerator(claude, db, reddit_base=reddit_base)
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

@app.route("/api/analytics/deployments")
def api_deployment_analytics():
    db = get_db()
    try:
        sid = request.args.get("subreddit_id", type=int)
        bid = request.args.get("brand_id", type=int)
        date_from = request.args.get("date_from") or None
        date_to = request.args.get("date_to") or None
        result = db.get_deployment_analytics(subreddit_id=sid, brand_id=bid, date_from=date_from, date_to=date_to)
        return jsonify(result)
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
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Subreddit name is required"}), 400
        # Check for duplicate name
        existing = db.conn.execute("SELECT id FROM subreddits WHERE LOWER(name) = LOWER(?)", (name,)).fetchone()
        if existing:
            return jsonify({"error": f"Subreddit '{name}' already exists"}), 409
        sid = db.create_subreddit(
            name=name,
            domain=data.get("domain", ""),
            description=data.get("description", ""),
            rules=data.get("rules", "[]"),
            sidebar=data.get("sidebar", ""),
            welcome_message=data.get("welcome_message", ""),
        )
        return jsonify({"id": sid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()

# ---------------------------------------------------------------------------
# API: Brands
# ---------------------------------------------------------------------------

def _extract_brand_enrichment_fields(data):
    """Pull the 7 GEO enrichment fields out of a request payload.

    List fields (use_cases/pain_points/features/competitors) are JSON-stringified
    for storage. Missing keys map to None so `update_brand` leaves them unchanged.
    """
    out = {}
    for scalar in ("category", "audience"):
        if scalar in data:
            val = data.get(scalar)
            out[scalar] = val.strip() if isinstance(val, str) else val
    for listf in ("use_cases", "pain_points", "features", "competitors"):
        if listf in data:
            v = data.get(listf)
            if v is None:
                out[listf] = None
            elif isinstance(v, list):
                out[listf] = json.dumps([str(x).strip() for x in v if str(x).strip()])
            elif isinstance(v, str):
                # Accept newline- or comma-separated free text from the form
                items = [s.strip() for s in v.replace("\n", ",").split(",") if s.strip()]
                out[listf] = json.dumps(items)
    if "enriched_at" in data:
        out["enriched_at"] = data.get("enriched_at")
    return out

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
        enrich_fields = _extract_brand_enrichment_fields(data)
        bid = db.add_brand(
            subreddit_id=sid,
            name=data["name"],
            domain_url=data.get("domain_url", ""),
            context=data.get("context", ""),
            keywords=json.dumps(data.get("keywords", [])),
            **enrich_fields,
        )
        return jsonify({"id": bid})
    finally:
        db.close()

@app.route("/api/brands", methods=["POST"])
def api_add_brand_standalone():
    db = get_db()
    try:
        data = request.json
        enrich_fields = _extract_brand_enrichment_fields(data)
        bid = db.add_brand(
            subreddit_id=data.get("subreddit_id") or None,
            name=data["name"],
            domain_url=data.get("domain_url", ""),
            context=data.get("context", ""),
            keywords=json.dumps(data.get("keywords", [])),
            **enrich_fields,
        )
        return jsonify({"id": bid})
    finally:
        db.close()

@app.route("/api/brands/<int:bid>", methods=["PUT"])
def api_update_brand(bid):
    db = get_db()
    try:
        data = request.json
        enrich_fields = _extract_brand_enrichment_fields(data)
        db.update_brand(
            brand_id=bid,
            context=data.get("context"),
            domain_url=data.get("domain_url"),
            keywords=json.dumps(data["keywords"]) if "keywords" in data else None,
            **enrich_fields,
        )
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/brands/enrich", methods=["POST"])
def api_enrich_brand_draft():
    """Enrich a brand from its homepage + LLM. Returns a draft dict — does NOT save.

    Payload: { "name": str, "domain_url": str }
    Response: { category, audience, use_cases[], pain_points[], features[],
                competitors[], context_summary, _page_fetched }
    """
    from generators.brand_enrichment import enrich_brand
    data = request.json or {}
    name = (data.get("name") or "").strip()
    domain_url = (data.get("domain_url") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    claude = ClaudeClient(ANTHROPIC_API_KEY)
    draft = enrich_brand(claude, name, domain_url)
    if not draft:
        return jsonify({
            "error": "Enrichment failed — LLM returned no usable data. "
                     "Check the URL and try again, or fill fields manually."
        }), 502
    return jsonify(draft)

@app.route("/api/brands/<int:bid>/enrich", methods=["POST"])
def api_enrich_existing_brand(bid):
    """Enrich an existing brand by its ID. Uses the brand's stored name + domain_url.
    Returns a draft dict (does NOT save)."""
    from generators.brand_enrichment import enrich_brand
    db = get_db()
    try:
        brand = db.get_brand(bid)
        if not brand:
            return jsonify({"error": "brand not found"}), 404
    finally:
        db.close()

    claude = ClaudeClient(ANTHROPIC_API_KEY)
    draft = enrich_brand(claude, brand["name"], brand.get("domain_url") or "")
    if not draft:
        return jsonify({
            "error": "Enrichment failed — LLM returned no usable data. "
                     "Check the brand's domain URL and try again."
        }), 502
    return jsonify(draft)

# ---------------------------------------------------------------------------
# API: Posts
# ---------------------------------------------------------------------------

@app.route("/api/posts/all")
def api_all_posts():
    db = get_db()
    try:
        posts = db.get_all_posts(
            brand_id=request.args.get("brand_id", type=int),
            subreddit_id=request.args.get("subreddit_id", type=int),
            status=request.args.get("status") or None,
            date=request.args.get("date") or None,
            limit=200,
        )
        return jsonify(posts)
    finally:
        db.close()

@app.route("/api/subreddits/<int:sid>/posts")
def api_list_posts(sid):
    db = get_db()
    try:
        brand_id = request.args.get("brand_id", type=int)
        include_filler = request.args.get("include_filler", "true") == "true"
        date = request.args.get("date") or None
        posts = db.get_posts_with_details(sid, brand_id=brand_id, include_filler=include_filler, limit=200, date=date)
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
        post["reddit_url"] = db.get_url_for_post(pid) or ""
        post["comment_count"] = db.conn.execute(
            "SELECT COUNT(*) FROM comments WHERE post_id = ?", (pid,)
        ).fetchone()[0]
        brands = db.get_brands_for_post(pid)
        post["brands"] = [{"id": b["id"], "name": b["name"]} for b in brands]
        post["brand_names"] = ", ".join(b["name"] for b in brands) if brands else ""
        return jsonify(post)
    finally:
        db.close()

@app.route("/api/posts/custom", methods=["POST"])
def api_add_custom_post():
    db = get_db()
    try:
        data = request.json
        sid = data.get("subreddit_id")
        title = data.get("title", "").strip()
        body = data.get("body", "").strip()
        storyline = data.get("storyline", "custom")
        day = data.get("suggested_post_day", 0)
        brand_ids = data.get("brand_ids", [])
        if not sid or not title or not body:
            return jsonify({"error": "subreddit_id, title, and body are required"}), 400
        post_id = db.save_post(
            subreddit_id=sid,
            brand_id=brand_ids[0] if brand_ids else None,
            title=title, body=body, storyline=storyline,
            is_custom=1, status="draft",
            suggested_post_day=day,
            brand_ids=brand_ids if brand_ids else None,
        )
        return jsonify({"ok": True, "post_id": post_id})
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
        # Set deployed_at timestamp
        from datetime import datetime as _dt
        db.conn.execute("UPDATE posts SET deployed_at = ? WHERE id = ?", (_dt.now().strftime("%Y-%m-%d %H:%M:%S"), pid))
        db.conn.commit()
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

@app.route("/api/auto-assign", methods=["POST"])
def api_auto_assign():
    from auto_assign import auto_assign_post
    db = get_db()
    try:
        data = request.json
        post_ids = data.get("post_ids") or ([data["post_id"]] if "post_id" in data else [])
        exclude = data.get("exclude_accounts", [])
        if not post_ids:
            return jsonify({"error": "post_id or post_ids required"}), 400
        results = []
        for pid in post_ids:
            result = auto_assign_post(db, pid, exclude_accounts=exclude)
            result["post_id"] = pid
            results.append(result)
        has_error = any(r.get("error") for r in results)
        return jsonify({"ok": not has_error, "results": results})
    finally:
        db.close()

@app.route("/api/auto-assign-posts", methods=["POST"])
def api_auto_assign_posts():
    from auto_assign import auto_assign_posts
    db = get_db()
    try:
        data = request.json
        subreddit_id = data.get("subreddit_id")
        if not subreddit_id:
            return jsonify({"error": "subreddit_id required"}), 400
        exclude = data.get("exclude_accounts", [])
        result = auto_assign_posts(db, subreddit_id, exclude_accounts=exclude)
        return jsonify({"ok": not result.get("error"), **result})
    finally:
        db.close()

@app.route("/api/comments/<int:cid>/auto-assign", methods=["POST"])
def api_auto_assign_single_comment(cid):
    from auto_assign import auto_assign_single_comment
    db = get_db()
    try:
        data = request.json or {}
        exclude = data.get("exclude_accounts")
        result = auto_assign_single_comment(db, cid, exclude_accounts=exclude)
        return jsonify(result), 200 if result.get("ok") else 400
    finally:
        db.close()

@app.route("/api/posts/<int:pid>/auto-assign", methods=["POST"])
def api_auto_assign_single_post(pid):
    from auto_assign import auto_assign_single_post
    db = get_db()
    try:
        data = request.json or {}
        exclude = data.get("exclude_accounts")
        result = auto_assign_single_post(db, pid, exclude_accounts=exclude)
        return jsonify(result), 200 if result.get("ok") else 400
    finally:
        db.close()

@app.route("/api/search/comments/<int:cid>/auto-assign", methods=["POST"])
def api_auto_assign_single_search_comment(cid):
    """Auto-assign a single search comment using scoring logic."""
    from auto_assign import auto_assign_single_search_comment
    db = get_db()
    try:
        data = request.json or {}
        exclude = data.get("exclude_accounts", [])
        result = auto_assign_single_search_comment(db, cid, exclude_accounts=exclude)
        return jsonify(result), 200 if result.get("ok") else 400
    finally:
        db.close()


@app.route("/api/search/auto-assign", methods=["POST"])
def api_auto_assign_search_comments():
    """Auto-assign all draft search comments using scoring logic."""
    from auto_assign import auto_assign_search_comments
    db = get_db()
    try:
        data = request.json or {}
        exclude = data.get("exclude_accounts", [])
        result = auto_assign_search_comments(db, exclude_accounts=exclude)
        return jsonify(result), 200 if not result.get("error") else 400
    finally:
        db.close()


@app.route("/api/posts/<int:pid>/unassign-all", methods=["POST"])
def api_unassign_all_for_post(pid):
    db = get_db()
    try:
        db.bulk_unassign_all_for_post(pid)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/posts/<int:pid>/unassign-owner", methods=["POST"])
def api_unassign_post_owner(pid):
    db = get_db()
    try:
        db.unassign_post_owner(pid)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/subreddits/<int:sid>/unassign-all-posts", methods=["POST"])
def api_unassign_all_posts(sid):
    db = get_db()
    try:
        count = db.bulk_unassign_posts_in_subreddit(sid)
        return jsonify({"ok": True, "unassigned": count})
    finally:
        db.close()

@app.route("/api/subreddits/<int:sid>/unassign-all-comments", methods=["POST"])
def api_unassign_all_comments(sid):
    db = get_db()
    try:
        count = db.bulk_unassign_comments_in_subreddit(sid)
        return jsonify({"ok": True, "unassigned": count})
    finally:
        db.close()

@app.route("/api/comments/<int:cid>/body", methods=["PUT"])
def api_update_comment_body(cid):
    db = get_db()
    try:
        data = request.json
        body = data.get("body", "").strip()
        if not body:
            return jsonify({"error": "Body cannot be empty"}), 400
        db.update_comment_body(cid, body)
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

@app.route("/api/comments/<int:cid>/url", methods=["PATCH"])
def api_update_comment_url(cid):
    db = get_db()
    try:
        url = request.json.get("reddit_comment_url", "")
        if not url:
            return jsonify({"error": "URL required"}), 400
        db.update_comment_url(cid, url)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/search/comments/<int:cid>/url", methods=["PATCH"])
def api_update_search_comment_url(cid):
    db = get_db()
    try:
        url = request.json.get("reddit_comment_url", "")
        if not url:
            return jsonify({"error": "URL required"}), 400
        db.update_search_comment_url(cid, url)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/comments/<int:cid>/undeploy", methods=["POST"])
def api_undeploy_comment(cid):
    db = get_db()
    try:
        db.undeploy_comment(cid)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/comments/<int:cid>/inform", methods=["POST"])
def api_inform_comment(cid):
    db = get_db()
    try:
        db.inform_comment(cid)
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

@app.route("/api/comments/<int:cid>/mark-removed", methods=["POST"])
def api_mark_comment_removed(cid):
    db = get_db()
    try:
        db.mark_comment_removed(cid)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/comments/<int:cid>/unremove", methods=["POST"])
def api_unremove_comment(cid):
    db = get_db()
    try:
        db.unremove_comment(cid)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/comments/<int:cid>/mark-paid", methods=["POST"])
def api_mark_comment_paid(cid):
    db = get_db()
    try:
        db.mark_comment_paid(cid)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/posts/<int:pid>/undeploy", methods=["POST"])
def api_undeploy_post(pid):
    db = get_db()
    try:
        db.undeploy_post(pid)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/posts/<int:pid>/mark-paid", methods=["POST"])
def api_mark_post_paid(pid):
    db = get_db()
    try:
        db.mark_post_paid(pid)
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
        brand_id = request.args.get("brand_id")
        sort_by = request.args.get("sort_by")
        mb = None
        if mentions_brand == "1":
            mb = True
        elif mentions_brand == "0":
            mb = False
        comments = db.get_filtered_comments(
            sid, status=status or None, mentions_brand=mb,
            account_id=account_id or None,
            brand_id=int(brand_id) if brand_id else None,
            sort_by=sort_by or None
        )
        return jsonify(comments)
    finally:
        db.close()

@app.route("/api/brands/<int:bid>/all-comments")
def api_brand_all_comments(bid):
    """Get all comments (regular + search) for a brand across all subreddits."""
    db = get_db()
    try:
        status = request.args.get("status")
        sort_by = request.args.get("sort_by")
        comments = db.get_all_comments_by_brand(
            bid, status=status or None, sort_by=sort_by or None
        )
        return jsonify(comments)
    finally:
        db.close()


@app.route("/api/all-comments")
def api_global_all_comments():
    """Get all comments (regular + search) globally with filters and pagination."""
    db = get_db()
    try:
        status = request.args.get("status") or None
        brand_id = request.args.get("brand_id")
        subreddit_id = request.args.get("subreddit_id")
        account_id = request.args.get("account_id") or None
        sort_by = request.args.get("sort_by") or None
        source = request.args.get("source") or None
        limit = int(request.args.get("limit", 200))
        offset = int(request.args.get("offset", 0))
        date = request.args.get("date") or None
        result = db.get_all_comments_global(
            status=status,
            brand_id=int(brand_id) if brand_id else None,
            subreddit_id=int(subreddit_id) if subreddit_id else None,
            account_id=account_id,
            sort_by=sort_by,
            source=source,
            date=date,
            limit=limit,
            offset=offset,
        )
        return jsonify(result)
    finally:
        db.close()


def _check_live_batch(deployed, db, log_prefix="CHECK-LIVE"):
    """Shared live-check logic for a batch of comments.
    Each item must have: id, reddit_comment_url, source ('comment' or 'search_comment').
    Returns dict with checked/live/dead/errors counts and error_details breakdown.
    """
    import time as _time
    import requests as _requests
    checked = 0
    live = 0
    dead = 0
    errors = 0
    error_details = {"forbidden": 0, "rate_limited": 0, "bad_url": 0,
                     "timeout": 0, "non_json": 0, "http_other": 0, "exception": 0}

    def _mark_dead(item):
        src = item.get("source", "comment")
        if src == "comment":
            db.mark_comment_deleted(item["id"])
        else:
            db.mark_search_comment_removed(item["id"])

    def _mark_live(item):
        src = item.get("source", "comment")
        if src == "comment":
            db.set_comment_live_check(item["id"])
        else:
            db.set_search_comment_live_check(item["id"])

    for item in deployed:
        checked += 1
        raw_url = item["reddit_comment_url"]
        src = item.get("source", "comment")
        if not raw_url:
            print(f"[{log_prefix}] Skipping #{item['id']} ({src}): no URL", flush=True)
            errors += 1
            error_details["bad_url"] += 1
            continue

        # Clean URL → extract path → append .json → route through proxy
        clean_url = raw_url.strip().split("?")[0].rstrip("/")
        if "/comment/" not in clean_url and "/comments/" not in clean_url:
            print(f"[{log_prefix}] Skipping #{item['id']} ({src}): not a comment URL: {clean_url}", flush=True)
            errors += 1
            error_details["bad_url"] += 1
            continue

        # Extract path from URL (strip domain) and append .json
        import re as _re
        path = _re.sub(r'^https?://(?:www\.|old\.|new\.|np\.|m\.)?reddit\.com', '', clean_url)
        if not path.startswith('/'):
            print(f"[{log_prefix}] Skipping #{item['id']} ({src}): bad path: {clean_url}", flush=True)
            errors += 1
            error_details["bad_url"] += 1
            continue
        json_path = path + ".json"

        try:
            # Use _reddit_get which routes through Cloudflare proxy (avoids
            # Railway cloud IP being 403'd by Reddit directly)
            resp = _reddit_get(json_path, timeout=15)

            if resp.status_code == 404:
                print(f"[{log_prefix}] #{item['id']} ({src}) 404 — removed", flush=True)
                _mark_dead(item)
                dead += 1
                _time.sleep(3)
                continue
            if resp.status_code == 403:
                print(f"[{log_prefix}] #{item['id']} ({src}) 403 Forbidden", flush=True)
                errors += 1
                error_details["forbidden"] += 1
                _time.sleep(3)
                continue
            if resp.status_code == 429:
                print(f"[{log_prefix}] #{item['id']} ({src}) rate limited", flush=True)
                errors += 1
                error_details["rate_limited"] += 1
                _time.sleep(5)
                continue
            if resp.status_code != 200:
                print(f"[{log_prefix}] #{item['id']} ({src}) HTTP {resp.status_code}", flush=True)
                errors += 1
                error_details["http_other"] += 1
                _time.sleep(3)
                continue

            # Parse JSON — if it fails (HTML returned), skip as error
            try:
                data = resp.json()
            except (ValueError, Exception):
                ct = resp.headers.get("Content-Type", "")
                print(f"[{log_prefix}] #{item['id']} ({src}) JSON parse failed ({ct}), skipping", flush=True)
                errors += 1
                error_details["non_json"] += 1
                _time.sleep(3)
                continue

            # Reddit returns [post_data, comment_data]
            if not isinstance(data, list) or len(data) < 2:
                print(f"[{log_prefix}] #{item['id']} ({src}) unexpected JSON structure, skipping", flush=True)
                errors += 1
                error_details["non_json"] += 1
                _time.sleep(3)
                continue

            children = data[1].get("data", {}).get("children", [])
            if not children:
                print(f"[{log_prefix}] #{item['id']} ({src}) empty children — removed", flush=True)
                _mark_dead(item)
                dead += 1
                _time.sleep(3)
                continue

            comment = children[0].get("data", {})
            body = comment.get("body", "")
            author = comment.get("author", "")

            if body in ("[deleted]", "[removed]"):
                print(f"[{log_prefix}] #{item['id']} ({src}) {body}", flush=True)
                _mark_dead(item)
                dead += 1
            elif not body and author == "[deleted]":
                print(f"[{log_prefix}] #{item['id']} ({src}) user deleted", flush=True)
                _mark_dead(item)
                dead += 1
            else:
                print(f"[{log_prefix}] #{item['id']} ({src}) live (author={author})", flush=True)
                _mark_live(item)
                live += 1

        except (_requests.exceptions.Timeout, _requests.exceptions.ConnectionError) as e:
            print(f"[{log_prefix}] #{item['id']} ({src}) {type(e).__name__}: {e}", flush=True)
            errors += 1
            error_details["timeout"] += 1
        except Exception as e:
            print(f"[{log_prefix}] #{item['id']} ({src}) error: {e}", flush=True)
            errors += 1
            error_details["exception"] += 1
        _time.sleep(3)

    return {"checked": checked, "live": live, "dead": dead, "errors": errors,
            "error_details": error_details}


@app.route("/api/all-comments/check-live", methods=["POST"])
def api_check_live_all_comments():
    """Check all deployed/paid comments (regular + search) against Reddit."""
    def task():
        db = Database(DB_PATH)
        db.connect()
        db.initialize()
        try:
            deployed = db.get_all_deployed_comment_urls()
            return _check_live_batch(deployed, db, "CHECK-LIVE-ALL")
        finally:
            db.close()

    tid = start_task("check-live-all", task)
    return jsonify({"task_id": tid})


@app.route("/api/all-comments/mark-paid-all", methods=["POST"])
def api_mark_paid_all_comments():
    db = get_db()
    try:
        data = request.get_json() or {}
        updated = db.bulk_mark_paid(
            brand_id=data.get("brand_id"),
            subreddit_id=data.get("subreddit_id"),
            account_id=data.get("account_id"),
            source=data.get("source"),
            date=data.get("date"),
        )
        return jsonify({"updated": updated})
    finally:
        db.close()


@app.route("/api/subreddits/<int:sid>/check-live", methods=["POST"])
def api_check_live(sid):
    def task():
        db = Database(DB_PATH)
        db.connect()
        db.initialize()
        try:
            deployed = db.get_deployed_comment_urls(sid)
            # Add source field for shared helper
            for item in deployed:
                item["source"] = "comment"
            return _check_live_batch(deployed, db, "CHECK-LIVE")
        finally:
            db.close()

    tid = start_task("check-live", task)
    return jsonify({"task_id": tid})

@app.route("/api/search/comments/check-live", methods=["POST"])
def api_check_live_search_comments():
    """Check deployed search comments against Reddit to find removed/deleted ones.
    Optional body: { comment_ids: [1,2,3] } to check only specific comments.
    """
    data = request.json or {}
    filter_ids = data.get("comment_ids")

    def task():
        db = Database(DB_PATH)
        db.connect()
        db.initialize()
        try:
            deployed = db.get_deployed_search_comment_urls()
            if filter_ids:
                id_set = set(filter_ids)
                deployed = [d for d in deployed if d["id"] in id_set]
            for item in deployed:
                item["source"] = "search_comment"
            return _check_live_batch(deployed, db, "CHECK-LIVE-SEARCH")
        finally:
            db.close()

    tid = start_task("check-live-search", task)
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

@app.route("/api/calendar/events")
def api_calendar_events():
    """Get unified calendar events: published posts + assigned/deployed comments."""
    db = get_db()
    try:
        events = db.get_calendar_events(
            date_from=request.args.get("date_from"),
            date_to=request.args.get("date_to"),
            brand_id=request.args.get("brand_id") or None,
            subreddit_id=request.args.get("subreddit_id") or None,
            account_id=request.args.get("account_id") or None,
            status=request.args.get("status") or None,
            event_type=request.args.get("event_type") or None,
            ref=request.args.get("ref") or None,
        )
        return jsonify(events)
    finally:
        db.close()

@app.route("/api/calendar/account-summary")
def api_calendar_account_summary():
    """Get per-account counts for a given date."""
    date = request.args.get("date")
    if not date:
        return jsonify([])
    db = get_db()
    try:
        return jsonify(db.get_calendar_account_summary(
            date=date,
            brand_id=request.args.get("brand_id") or None,
            subreddit_id=request.args.get("subreddit_id") or None,
            ref=request.args.get("ref") or None,
        ))
    finally:
        db.close()

@app.route("/api/resolve-share-url", methods=["POST"])
def api_resolve_share_url():
    """Resolve a Reddit /s/ share URL to canonical URL server-side."""
    import requests as _requests
    url = (request.json or {}).get("url", "")
    if not url or "/s/" not in url:
        return jsonify({"error": "Not a share URL"}), 400
    try:
        resp = _requests.get(url, allow_redirects=True, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        })
        resolved = resp.url.split("?")[0].rstrip("/")
        if "/comments/" in resolved:
            return jsonify({"resolved": resolved})
        # Reddit may have returned the page HTML — try to extract canonical URL from it
        import re
        match = re.search(r'<link rel="canonical" href="(https://www\.reddit\.com/r/[^"]+)"', resp.text[:5000])
        if match:
            canonical = match.group(1).split("?")[0].rstrip("/")
            return jsonify({"resolved": canonical})
        return jsonify({"error": "Could not resolve", "final_url": resolved}), 422
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/comments/live-stats", methods=["POST"])
def api_comments_live_stats():
    """Fetch live Reddit stats (upvotes, replies) for a list of comment URLs server-side."""
    import time as _time

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
                # Share URLs — skip, should be resolved via /api/resolve-share-url first
                if "/s/" in clean:
                    results[str(cid)] = {"liveness": "share_link"}
                    continue
                path = clean.replace("https://www.reddit.com", "") + ".json"
                resp = _reddit_get(path)
                if resp.status_code == 404:
                    results[str(cid)] = {
                        "score": 0, "author": "", "num_replies": 0,
                        "permalink": "", "created_utc": 0,
                        "liveness": "removed",
                    }
                elif resp.status_code == 200:
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
                                body_text = cd.get("body", "")
                                is_removed = body_text in ("[deleted]", "[removed]")
                                results[str(cid)] = {
                                    "score": cd.get("score", 0),
                                    "author": cd.get("author", ""),
                                    "num_replies": num_replies,
                                    "permalink": cd.get("permalink", ""),
                                    "created_utc": cd.get("created_utc", 0),
                                    "liveness": "removed" if is_removed else "live",
                                }
                                break
            except Exception as e:
                print(f"[LIVE-STATS] Error fetching {url}: {e}", flush=True)
            _time.sleep(2)
        return results

    tid = start_task("live-stats", task)
    return jsonify({"task_id": tid})

@app.route("/api/comments/<int:cid>")
def api_get_comment(cid):
    db = get_db()
    try:
        comment = db.get_comment(cid)
        if not comment:
            return jsonify({"error": "Not found"}), 404
        return jsonify(comment)
    finally:
        db.close()

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

        all_comments = [dict(r) for r in db.conn.execute(
            """SELECT c.* FROM comments c
               JOIN posts p ON c.post_id = p.id
               WHERE p.subreddit_id = ?""",
            (sid,)
        ).fetchall()]

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
            count = data.get("count", 5)
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
    from config import POST_BATCH_SIZES
    data = request.json
    count = int(data.get("count", 3))
    if count not in POST_BATCH_SIZES:
        return jsonify({
            "error": f"count must be one of {list(POST_BATCH_SIZES)} (GEO batches are "
                     f"strict 1:1:1 commercial/comparison/informational)"
        }), 400

    def task():
        db, claude, _, post_gen, _ = make_generators()
        try:
            sub = db.get_subreddit(data["subreddit_id"])
            # Support multi-brand: brand_ids list OR single brand_id
            brand_ids = data.get("brand_ids") or ([data["brand_id"]] if data.get("brand_id") else [])
            brands = [db.get_brand(bid) for bid in brand_ids]
            brands = [b for b in brands if b]  # filter None
            if not sub or not brands:
                raise ValueError("Subreddit or brand(s) not found")
            posts = post_gen.generate_posts(sub, brands, count)
            return [
                {
                    "id": p["id"],
                    "title": p["title"],
                    "storyline": p.get("storyline", ""),
                    "intent": p.get("intent") or "",
                }
                for p in posts
            ]
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
            if not post:
                raise ValueError("Post not found")
            num_comments = data.get("count", 5)

            # Support multi-brand: brands_config OR single brand_id
            brands_config_raw = data.get("brands_config", [])
            if brands_config_raw:
                # Multi-brand mode: [{brand_id: 1, mention_count: 2}, ...]
                brands_config = []
                for bc in brands_config_raw:
                    brand = db.get_brand(bc["brand_id"])
                    if brand:
                        brands_config.append({"brand": brand, "mention_count": bc.get("mention_count", 1)})
                if not brands_config:
                    raise ValueError("No valid brands found in brands_config")
                comments = comment_gen.generate_comment_tree(
                    post, None, num_comments,
                    post_day_offset=post.get("suggested_post_day", 0),
                    brands_config=brands_config,
                    op_reply_count=data.get("op_reply_count", 0),
                )
            else:
                # Single-brand backward compat
                brand = db.get_brand(data["brand_id"])
                if not brand:
                    raise ValueError("Brand not found")
                ratio = data.get("brand_mention_ratio", DEFAULT_BRAND_MENTION_RATIO)
                comments = comment_gen.generate_comment_tree(
                    post, brand, num_comments,
                    brand_mention_ratio=ratio,
                    post_day_offset=post.get("suggested_post_day", 0),
                    op_reply_count=data.get("op_reply_count", 0),
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

@app.route("/api/generate/op-replies", methods=["POST"])
def api_gen_op_replies():
    data = request.json

    def task():
        db, claude, _, _, comment_gen = make_generators()
        try:
            post = db.get_post(data["post_id"])
            if not post:
                raise ValueError("Post not found")
            # Brand is optional — use first brand associated with the post
            brand = None
            if data.get("brand_id"):
                brand = db.get_brand(data["brand_id"])
            if not brand:
                brands = db.get_brands_for_post(post["id"])
                brand = brands[0] if brands else None
            count = data.get("count", 3)
            comments = comment_gen.generate_op_replies(
                post, brand, num_replies=count,
                post_day_offset=post.get("suggested_post_day", 0),
            )
            return [{"id": c["id"], "body": c["body"][:100]} for c in comments]
        finally:
            db.close()

    tid = start_task("op-replies", task)
    return jsonify({"task_id": tid})

@app.route("/api/generate/reply-to-comment", methods=["POST"])
def api_gen_reply_to_comment():
    """Generate a single reply to a specific existing comment."""
    data = request.json
    comment_id = data.get("comment_id")
    brand_id = data.get("brand_id")
    mention_brand = data.get("mention_brand", False)

    if not comment_id:
        return jsonify({"error": "comment_id required"}), 400

    db = get_db()
    try:
        comment = db.get_comment(comment_id)
        if not comment:
            return jsonify({"error": "Comment not found"}), 404

        post = db.get_post(comment["post_id"])
        if not post:
            return jsonify({"error": "Post not found"}), 404

        brand = db.get_brand(brand_id) if brand_id else None
        if not brand:
            brands = db.get_brands_for_post(post["id"])
            brand = brands[0] if brands else None
        if not brand:
            return jsonify({"error": "No brand found for this post"}), 400

        subreddit = db.get_subreddit(post["subreddit_id"])

        from generators.comment_gen import CommentGenerator
        from generators.base import ClaudeClient
        from config import ANTHROPIC_API_KEY, PROMPT_VERSION
        claude = ClaudeClient(ANTHROPIC_API_KEY)
        comment_gen = CommentGenerator(db, claude)

        mock_tone = {
            "formality": "casual to semi-formal",
            "humor_style": "occasional dry humor",
            "technical_level": "moderate",
            "common_phrases": [],
            "overall_vibe": "helpful community discussion",
            "sentence_structure": "mix of short and medium",
            "capitalization": "mostly lowercase with normal caps",
            "punctuation_style": "casual, minimal",
            "emotional_tone": "generally supportive",
        }
        mock_stats = {"avg_chars": 300, "avg_words": 60, "median_chars": 250, "min_chars": 50, "max_chars": 600}

        target = {
            "body": comment["body"],
            "score": 5,
            "author": comment.get("account_id") or "community_member",
            "id": "",
            "permalink": "",
        }

        result = comment_gen.generate_comments(
            post_title=post["title"],
            post_body=post.get("body", ""),
            subreddit=subreddit["name"],
            comments=[target],
            brand_name=brand["name"],
            brand_context=brand.get("context", ""),
            num_comments=1,
            tone_analysis=mock_tone,
            comment_stats=mock_stats,
            mention_brand_flags=[mention_brand],
            reply_targets={0: target},
            relevance={"best_angle": "replying to comment", "natural_fit": 2},
            brand_assignments=[brand if mention_brand else None],
            all_brand_names=[brand["name"]],
        )

        bodies = result.get("generated_comments", [])
        if not bodies:
            return jsonify({"error": "Failed to generate reply"})

        body = bodies[0]
        mentions = mention_brand and brand["name"].lower() in body.lower()
        r_personas = result.get("_personas", [])
        r_structures = result.get("_structures", [])

        # Use the parent's day + 1 for scheduling
        parent_day = comment.get("suggested_post_day", 0) or 0

        new_id = db.save_comment(
            post_id=post["id"],
            brand_id=brand["id"],
            body=body,
            persona_id=r_personas[0] if r_personas else None,
            structure_id=r_structures[0] if r_structures else None,
            is_reply=1,
            parent_comment_id=comment_id,
            mentions_brand=1 if mentions else 0,
            status="complete",
            suggested_post_day=parent_day + 1,
            suggested_order=0,
            prompt_version=PROMPT_VERSION,
        )

        return jsonify({
            "ok": True,
            "comment_id": new_id,
            "body": body[:200],
            "mentions_brand": mentions,
        })
    finally:
        db.close()


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
    db = get_db()
    try:
        t = db.get_task(task_id)
        if not t:
            return jsonify({"error": "Task not found"}), 404
        return jsonify(t)
    finally:
        db.close()

@app.route("/api/tasks/<task_id>/cancel", methods=["POST"])
def api_cancel_task(task_id):
    """Cancel a running background task by marking it as error in DB.
    The thread itself is a daemon and will eventually die, but the client
    will see it as cancelled immediately on next poll."""
    db = get_db()
    try:
        t = db.get_task(task_id)
        if not t:
            return jsonify({"error": "Task not found"}), 404
        if t["status"] == "running":
            db.update_task(task_id, "error", error="Cancelled by user")
        _task_threads.pop(task_id, None)
        return jsonify({"ok": True})
    finally:
        db.close()

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

@app.route("/api/brands/<name>/deployed-hierarchy")
def api_brand_deployed_hierarchy(name):
    """Get deployed posts and comments for a brand, grouped by subreddit."""
    db = get_db()
    try:
        return jsonify(db.get_brand_deployed_hierarchy(name))
    finally:
        db.close()

@app.route("/api/debug/brand/<name>/comments")
def api_debug_brand_comments(name):
    """Debug endpoint: show raw comment data for a brand to diagnose missing comments."""
    db = get_db()
    try:
        # All brand IDs for this name
        brand_ids = db.conn.execute(
            "SELECT id, name, subreddit_id FROM brands WHERE LOWER(name) = LOWER(?)", (name,)
        ).fetchall()
        brand_ids_list = [r["id"] for r in brand_ids]

        # Raw comment counts per brand_id
        raw_comments = []
        for bid in brand_ids_list:
            rows = db.conn.execute(
                "SELECT c.id, c.brand_id, c.post_id, c.status, c.account_id, c.is_ours, c.created_at FROM comments c WHERE c.brand_id = ?", (bid,)
            ).fetchall()
            for r in rows:
                d = dict(r)
                # Check if post exists
                post = db.conn.execute("SELECT id, title, subreddit_id FROM posts WHERE id = ?", (d["post_id"],)).fetchone()
                d["post_exists"] = post is not None
                d["post_title"] = dict(post)["title"] if post else None
                d["post_subreddit_id"] = dict(post)["subreddit_id"] if post else None
                raw_comments.append(d)

        # Raw search_comments
        raw_search = []
        for bid in brand_ids_list:
            rows = db.conn.execute(
                "SELECT sc.id, sc.brand_id, sc.search_post_id, sc.status, sc.account_id, sc.created_at FROM search_comments sc WHERE sc.brand_id = ?", (bid,)
            ).fetchall()
            raw_search.extend([dict(r) for r in rows])

        # What get_brand_comments_with_details returns
        unified = db.get_brand_comments_with_details(name)

        return jsonify({
            "brand_name": name,
            "brand_entries": [dict(r) for r in brand_ids],
            "brand_ids": brand_ids_list,
            "raw_comments": raw_comments,
            "raw_comments_count": len(raw_comments),
            "raw_search_comments": raw_search,
            "raw_search_count": len(raw_search),
            "unified_result": unified,
            "unified_count": len(unified),
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
# Live Search — Search, Save, Generate, Manage
# ---------------------------------------------------------------------------

def _resolve_brand_keywords(data, db=None):
    """Resolve a list of search keywords from brand info.

    Input dict fields (any subset):
      - brand_id:       int, if provided and brand has stored keywords + no
                        force_regenerate, returns cached keywords.
      - brand_name:     str, used for ad-hoc or regen.
      - brand_context:  str, used for ad-hoc or regen.
      - force_regenerate: bool, ignore cached keywords and call Claude fresh.

    Returns: (keywords: list[str], source: "cached" | "generated")
    Raises: ValueError on missing inputs or Claude failure.
    """
    brand_id = data.get("brand_id")
    brand_name = (data.get("brand_name") or "").strip()
    brand_context = (data.get("brand_context") or "").strip()
    force_regen = bool(data.get("force_regenerate"))

    own_db = False
    if brand_id and db is None:
        db = Database(DB_PATH)
        db.connect()
        own_db = True

    try:
        brand_row = None
        if brand_id:
            brand_row = db.get_brand(int(brand_id))
            if not brand_row:
                raise ValueError(f"brand_id {brand_id} not found")
            if not brand_name:
                brand_name = brand_row.get("name") or ""
            if not brand_context:
                brand_context = brand_row.get("context") or ""

            # Cached keywords path
            if not force_regen:
                try:
                    stored = json.loads(brand_row.get("keywords") or "[]")
                except Exception:
                    stored = []
                if isinstance(stored, list) and len(stored) >= 3 and all(isinstance(k, str) for k in stored):
                    return stored, "cached"

        if not brand_name and not brand_context:
            raise ValueError("brand_id or (brand_name + brand_context) required")

        # Generate via Claude
        api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        import anthropic
        import re
        client = anthropic.Anthropic(api_key=api_key)
        prompt = (
            "You are generating Reddit search queries for a brand. Given the brand "
            "name and context, return 6 diverse SHORT search queries (2-5 words each) "
            "that cover different user intents: pain-points people post about, "
            "competitor/alternative searches, 'looking for' / recommendation posts, "
            "comparison questions, and use-case descriptions. Prefer phrasing real "
            "Reddit users would write. Avoid the brand name itself.\n\n"
            f"Brand: {brand_name or '(unnamed)'}\n"
            f"Context: {brand_context or '(none)'}\n\n"
            "Return ONLY a JSON array of 6 lowercase strings. No other text."
        )
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        text = resp.content[0].text.strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        raw = json.loads(match.group() if match else text)
        keywords = [str(k).strip() for k in raw if str(k).strip()]
        if not keywords:
            raise ValueError("Claude returned no usable keywords")
        # Cap at 8 to bound API load
        keywords = keywords[:8]

        # Persist back onto the brand for free reuse
        if brand_id:
            try:
                db.update_brand(int(brand_id), keywords=json.dumps(keywords))
            except Exception as e:
                print(f"⚠ failed to cache keywords on brand {brand_id}: {e}")

        return keywords, "generated"
    finally:
        if own_db and db is not None:
            db.close()


@app.route("/api/search/generate-brand-keywords", methods=["POST"])
def api_generate_brand_keywords():
    """Generate (or return cached) Reddit search keywords for a brand."""
    data = request.json or {}
    try:
        keywords, source = _resolve_brand_keywords(data)
        return jsonify({"keywords": keywords, "source": source})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"generation failed: {e}"}), 500


@app.route("/api/search/suggest-subreddits", methods=["POST"])
def api_suggest_subreddits():
    """Suggest active subreddits for a keyword/brand using Claude."""
    data = request.json or {}
    keyword = data.get("keyword", "").strip()
    brand_name = data.get("brand_name", "").strip()
    if not keyword and not brand_name:
        return jsonify({"error": "keyword or brand_name required"}), 400

    def task():
        api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        prompt = f"Suggest 5 active Reddit subreddits where someone could find posts related to: {keyword or brand_name}."
        if brand_name:
            prompt += f" The brand is '{brand_name}'."
        prompt += "\nReturn ONLY a JSON array of objects with 'name' (without r/) and 'reason' (1 sentence why). No other text."
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        text = resp.content[0].text.strip()
        # Extract JSON from response
        import re
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            suggestions = json.loads(match.group())
        else:
            suggestions = json.loads(text)
        # Check availability via Reddit
        proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
        for s in suggestions:
            try:
                r = _reddit_get(f"/r/{s['name']}/about.json")
                if r.status_code == 200:
                    data_r = r.json().get("data", {})
                    s["subscribers"] = data_r.get("subscribers", 0)
                    s["active"] = True
                else:
                    s["active"] = False
                    s["subscribers"] = 0
            except Exception:
                s["active"] = None
                s["subscribers"] = 0
        return suggestions

    tid = start_task("suggest-subreddits", task)
    return jsonify({"task_id": tid})


@app.route("/api/search/check-subreddit", methods=["GET"])
def api_check_subreddit():
    """Check if a subreddit exists and is active on Reddit."""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    try:
        r = _reddit_get(f"/r/{name}/about.json")
        if r.status_code == 200:
            try:
                json_data = r.json()
            except Exception:
                return jsonify({"exists": None, "error": "Invalid JSON response"})

            # Reddit returns kind=t5 for real subreddits
            data = {}
            if json_data.get("kind") == "t5":
                data = json_data.get("data", {})
            elif isinstance(json_data.get("data"), dict):
                data = json_data["data"]

            if data.get("display_name"):
                # Verify the display_name matches what we asked for
                # (Reddit can redirect to a different subreddit)
                display = data.get("display_name", "")
                return jsonify({
                    "exists": True,
                    "name": display or name,
                    "subscribers": data.get("subscribers", 0),
                    "active_accounts": data.get("accounts_active", 0),
                    "description": (data.get("public_description", "") or "")[:200],
                    "exact_match": display.lower() == name.lower(),
                })
            # 200 but no subreddit data — check if it's a listing/search page
            kind = json_data.get("kind", "")
            if kind in ("Listing", "listing") or not data:
                return jsonify({"exists": False})
            return jsonify({"exists": False})
        elif r.status_code == 404:
            return jsonify({"exists": False})
        elif r.status_code == 403:
            # 403 can mean: (a) private/quarantined sub, or (b) Reddit blocking our request
            # Try to distinguish by checking if the response body has subreddit info
            try:
                body = r.json()
                reason = body.get("reason", "")
                # Reddit returns {"reason": "private"} or {"reason": "quarantined"} for real subs
                if reason in ("private", "quarantined", "banned"):
                    return jsonify({
                        "exists": True,
                        "name": name,
                        "subscribers": 0,
                        "active_accounts": 0,
                        "description": f"({reason.capitalize()} subreddit)",
                    })
            except Exception:
                pass
            # Generic 403 — likely Reddit blocking, can't determine status
            return jsonify({"exists": None, "error": "Reddit returned 403 — could not verify"})
        else:
            return jsonify({"exists": None, "error": f"Reddit returned status {r.status_code}"})
    except Exception as e:
        return jsonify({"exists": None, "error": str(e)})


@app.route("/api/search/reddit", methods=["POST"])
def api_search_reddit():
    """Run a Reddit keyword search via RedditSearchBot (background task).

    Accepts either a manual `keyword` string OR an `auto_brand` dict that
    triggers multi-keyword search with keywords auto-generated from a brand's
    name/context (saved or ad-hoc).
    """
    from search.reddit_bot import RedditSearchBot
    import math
    data = request.json or {}
    keyword = (data.get("keyword") or "").strip()
    auto_brand = data.get("auto_brand")
    if not keyword and not auto_brand:
        return jsonify({"error": "keyword or auto_brand is required"}), 400
    if keyword and auto_brand:
        return jsonify({"error": "provide either keyword or auto_brand, not both"}), 400

    def task():
        proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
        bot = RedditSearchBot(reddit_base=proxy.rstrip("/") if proxy else None)
        task_db = Database(DB_PATH)
        task_db.connect()
        try:
            requested_limit = min(data.get("limit", 50), 200)
            # Use db_path (not db instance) so the scrutiny pass can open
            # per-thread SQLite connections when running concurrent sub-searches.
            common_filters = dict(
                subreddit=data.get("subreddit"),
                subreddits=data.get("subreddits"),
                excluded_subreddits=data.get("excluded_subreddits"),
                min_comments=data.get("min_comments", 0),
                min_score=data.get("min_score", 0),
                max_days_old=data.get("max_days_old"),
                sort_by=data.get("sort_by", "relevance"),
                sort_order=data.get("sort_order", "desc"),
                nsfw=data.get("nsfw"),
                min_upvote_ratio=data.get("min_upvote_ratio"),
                max_subscribers=data.get("max_subscribers"),
                min_subscribers=data.get("min_subscribers"),
                max_scrutiny=data.get("max_scrutiny"),
                db_path=DB_PATH,
            )

            if keyword:
                results = bot.search(keyword=keyword, limit=requested_limit, **common_filters)
                return {"results": results, "generated_keywords": None}

            # Auto-brand path
            keywords, source = _resolve_brand_keywords(auto_brand, db=task_db)
            n = max(len(keywords), 1)
            per_kw_limit = max(10, math.ceil(requested_limit / n * 1.5))
            print(f"    Auto-brand search: {n} keywords, per_kw_limit={per_kw_limit}, source={source}")
            print(f"    Keywords: {keywords}")
            merged = bot.search_multiple_keywords(
                keywords, concurrent=True, limit=per_kw_limit, **common_filters
            )
            # Trim merged (already sorted by score desc) to the requested limit
            trimmed = merged[:requested_limit]
            return {"results": trimmed, "generated_keywords": keywords, "keywords_source": source}
        finally:
            task_db.close()

    tid = start_task("search-reddit", task)
    return jsonify({"task_id": tid})


@app.route("/api/search/posts", methods=["GET"])
def api_list_search_posts():
    db = get_db()
    try:
        brand_id = request.args.get("brand_id", type=int)
        status = request.args.get("status")
        posts = db.list_search_posts(brand_id=brand_id, status=status)
        # Add comment counts
        for p in posts:
            row = db.conn.execute(
                "SELECT COUNT(*) as cnt FROM search_comments WHERE search_post_id = ? AND status != 'deleted'",
                (p["id"],)
            ).fetchone()
            p["comment_count"] = row["cnt"] if row else 0
        return jsonify(posts)
    finally:
        db.close()


@app.route("/api/search/posts", methods=["POST"])
def api_save_search_post():
    db = get_db()
    try:
        data = request.json
        if not data.get("reddit_url"):
            return jsonify({"error": "reddit_url is required"}), 400
        pid = db.save_search_post(data)
        if pid is None:
            return jsonify({"error": "Post already saved"}), 409
        return jsonify({"id": pid})
    finally:
        db.close()


@app.route("/api/search/posts/bulk", methods=["POST"])
def api_save_search_posts_bulk():
    db = get_db()
    try:
        items = request.json.get("posts", [])
        saved_ids = []
        dupes = 0
        for item in items:
            if not item.get("reddit_url"):
                continue
            pid = db.save_search_post(item)
            if pid:
                saved_ids.append(pid)
            else:
                dupes += 1
        return jsonify({"saved": len(saved_ids), "duplicates": dupes, "saved_ids": saved_ids})
    finally:
        db.close()


@app.route("/api/search/posts/<int:pid>", methods=["DELETE"])
def api_delete_search_post(pid):
    db = get_db()
    try:
        db.delete_search_post(pid)
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/search/posts/<int:pid>/brand", methods=["PUT"])
def api_update_search_post_brand(pid):
    db = get_db()
    try:
        data = request.json or {}
        brand_id = data.get("brand_id")
        c = db.conn.cursor()
        c.execute("UPDATE search_posts SET brand_id = ? WHERE id = ?", (brand_id, pid))
        db.conn.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/search/posts/<int:pid>/generate", methods=["POST"])
def api_generate_search_comments(pid):
    """Generate comments for a saved search post (background task)."""
    from generate.comment_generator import CommentGeneratorBot
    data = request.json or {}

    db_check = get_db()
    post = db_check.get_search_post(pid)
    if not post:
        db_check.close()
        return jsonify({"error": "Post not found"}), 404
    if post.get("status") == "generating":
        db_check.close()
        return jsonify({"error": "Already generating comments for this post"}), 400
    brand = db_check.get_brand(post["brand_id"]) if post.get("brand_id") else None
    db_check.close()

    if not brand:
        return jsonify({"error": "Post must have a brand assigned"}), 400

    brand_name = brand["name"]
    brand_context = brand["context"]
    brand_keywords = json.loads(brand.get("keywords", "[]")) if brand.get("keywords") else []
    num_comments = data.get("num_comments", 2)

    def task():
        proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
        api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
        bot = CommentGeneratorBot(api_key, reddit_base=proxy.rstrip("/") if proxy else None)

        db2 = Database(DB_PATH)
        db2.connect()
        db2.initialize()
        try:
            db2.update_search_post_status(pid, "generating")

            # Fetch comments from the Reddit post
            comments, post_body, is_archived = bot.fetch_comments(post["reddit_url"], limit=20)
            if is_archived:
                db2.update_search_post_status(pid, "saved")
                raise ValueError("Post is archived — cannot comment")
            if len(comments) < 1:
                db2.update_search_post_status(pid, "saved")
                raise ValueError(f"No comments found on this post — cannot analyze tone/context")

            comment_stats = bot._compute_comment_stats(comments)

            # Relevance check
            relevance = bot.check_relevance(
                post["title"], post_body, post["subreddit"],
                comments, brand_name, brand_context, brand_keywords
            )
            rel_score = relevance.get("score", 0)

            # Relevancy threshold — skip generation if score too low
            threshold = data.get("relevance_threshold", 6)
            if rel_score < threshold:
                db2.update_search_post_status(pid, "saved")
                return {
                    "skipped": True,
                    "relevance_score": rel_score,
                    "threshold": threshold,
                    "reason": f"Post relevance score ({rel_score}) is below threshold ({threshold}). Comments not generated."
                }

            # Tone analysis
            tone_analysis = bot.analyze_tone(
                post["title"], post_body, post["subreddit"],
                comments, comment_stats
            )

            # Reply targets
            reply_targets = {}
            if num_comments >= 3:
                reply_target = bot._select_reply_target(comments, post["title"], brand_name, relevance)
                if reply_target:
                    reply_targets[2] = reply_target

            # Generate
            generation = bot.generate_comments(
                post["title"], post_body, post["subreddit"],
                comments, brand_name, brand_context,
                num_comments=num_comments,
                tone_analysis=tone_analysis,
                comment_stats=comment_stats,
                relevance=relevance,
                reply_targets=reply_targets
            )

            generated = generation.get("generated_comments", [])
            if not generated:
                db2.update_search_post_status(pid, "saved")
                raise ValueError("Comment generation failed")

            # Brand mention enforcement — retry once if any comment misses the brand
            missing = [i+1 for i, c in enumerate(generated) if brand_name.lower() not in c.lower()]
            if missing:
                feedback = f"Comment(s) {missing} don't mention '{brand_name}'. Naturally weave in a mention."
                retry_gen = bot.generate_comments(
                    post["title"], post_body, post["subreddit"],
                    comments, brand_name, brand_context,
                    num_comments=num_comments,
                    tone_analysis=tone_analysis,
                    comment_stats=comment_stats,
                    retry_feedback=feedback,
                    relevance=relevance,
                    reply_targets=reply_targets
                )
                retry_comments = retry_gen.get("generated_comments", [])
                if retry_comments:
                    # Use retry if it has more brand mentions
                    retry_missing = [i for i, c in enumerate(retry_comments) if brand_name.lower() not in c.lower()]
                    if len(retry_missing) < len(missing):
                        generated = retry_comments
                        generation = retry_gen

            # Store generated comments
            stored = []
            for idx, body in enumerate(generated):
                is_reply = 1 if idx == 2 and reply_targets.get(2) else 0
                reply_url = None
                if is_reply and reply_targets.get(2):
                    rt = reply_targets[2]
                    reply_url = f"https://www.reddit.com{rt.get('permalink', '')}" if rt.get('permalink') else None

                mentions = 1 if brand_name.lower() in body.lower() else 0
                cid = db2.add_search_comment(
                    search_post_id=pid, body=body, brand_id=post.get("brand_id"),
                    persona_id=generation.get("config", {}).get(f"persona_{idx+1}"),
                    is_reply=is_reply, reply_to_url=reply_url,
                    mentions_brand=mentions, relevance_score=rel_score
                )
                stored.append({"id": cid, "body": body[:100]})

            db2.update_search_post_status(pid, "complete")
            return {"generated": len(stored), "comments": stored, "relevance_score": rel_score}
        finally:
            db2.close()

    tid = start_task("generate-search-comments", task)
    return jsonify({"task_id": tid})


@app.route("/api/search/posts/generate-batch", methods=["POST"])
def api_generate_search_comments_batch():
    """Generate comments for multiple search posts sequentially in one background task."""
    from generate.comment_generator import CommentGeneratorBot
    data = request.json or {}
    post_ids = data.get("post_ids", [])
    num_comments = data.get("num_comments", 2)

    if not post_ids:
        return jsonify({"error": "No post_ids provided"}), 400

    # Validate all posts upfront
    db_check = get_db()
    valid_posts = []
    for pid in post_ids:
        post = db_check.get_search_post(pid)
        if not post:
            continue
        if post.get("status") == "generating":
            continue
        brand = db_check.get_brand(post["brand_id"]) if post.get("brand_id") else None
        if not brand:
            continue
        valid_posts.append({
            "pid": pid, "post": post,
            "brand_name": brand["name"],
            "brand_context": brand["context"],
            "brand_keywords": json.loads(brand.get("keywords", "[]")) if brand.get("keywords") else []
        })
    db_check.close()

    if not valid_posts:
        return jsonify({"error": "No eligible posts to generate for"}), 400

    def task():
        proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
        api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
        bot = CommentGeneratorBot(api_key, reddit_base=proxy.rstrip("/") if proxy else None)

        db2 = Database(DB_PATH)
        db2.connect()
        db2.initialize()
        results = []
        try:
            for i, vp in enumerate(valid_posts):
                pid = vp["pid"]
                post = vp["post"]
                brand_name = vp["brand_name"]
                brand_context = vp["brand_context"]
                brand_keywords = vp["brand_keywords"]

                # Update progress
                progress_db = Database(DB_PATH)
                progress_db.connect()
                # We don't have a progress field, so store partial result
                progress_db.close()

                try:
                    db2.update_search_post_status(pid, "generating")

                    comments, post_body, is_archived = bot.fetch_comments(post["reddit_url"], limit=20)
                    if is_archived:
                        db2.update_search_post_status(pid, "saved")
                        results.append({"pid": pid, "skipped": True, "reason": "Post is archived"})
                        continue
                    if len(comments) < 1:
                        db2.update_search_post_status(pid, "saved")
                        results.append({"pid": pid, "skipped": True, "reason": "No comments found"})
                        continue

                    comment_stats = bot._compute_comment_stats(comments)

                    relevance = bot.check_relevance(
                        post["title"], post_body, post["subreddit"],
                        comments, brand_name, brand_context, brand_keywords
                    )
                    rel_score = relevance.get("score", 0)

                    threshold = data.get("relevance_threshold", 6)
                    if rel_score < threshold:
                        db2.update_search_post_status(pid, "saved")
                        results.append({"pid": pid, "skipped": True, "reason": f"Low relevance ({rel_score})"})
                        continue

                    tone_analysis = bot.analyze_tone(
                        post["title"], post_body, post["subreddit"],
                        comments, comment_stats
                    )

                    reply_targets = {}
                    if num_comments >= 3:
                        reply_target = bot._select_reply_target(comments, post["title"], brand_name, relevance)
                        if reply_target:
                            reply_targets[2] = reply_target

                    generation = bot.generate_comments(
                        post["title"], post_body, post["subreddit"],
                        comments, brand_name, brand_context,
                        num_comments=num_comments,
                        tone_analysis=tone_analysis,
                        comment_stats=comment_stats,
                        relevance=relevance,
                        reply_targets=reply_targets
                    )

                    generated = generation.get("generated_comments", [])
                    if not generated:
                        db2.update_search_post_status(pid, "saved")
                        results.append({"pid": pid, "error": "Generation failed"})
                        continue

                    # Brand mention enforcement retry
                    missing = [j+1 for j, c in enumerate(generated) if brand_name.lower() not in c.lower()]
                    if missing:
                        feedback = f"Comment(s) {missing} don't mention '{brand_name}'. Naturally weave in a mention."
                        retry_gen = bot.generate_comments(
                            post["title"], post_body, post["subreddit"],
                            comments, brand_name, brand_context,
                            num_comments=num_comments,
                            tone_analysis=tone_analysis,
                            comment_stats=comment_stats,
                            retry_feedback=feedback,
                            relevance=relevance,
                            reply_targets=reply_targets
                        )
                        retry_comments = retry_gen.get("generated_comments", [])
                        if retry_comments:
                            retry_missing = [j for j, c in enumerate(retry_comments) if brand_name.lower() not in c.lower()]
                            if len(retry_missing) < len(missing):
                                generated = retry_comments
                                generation = retry_gen

                    stored = []
                    for idx, body in enumerate(generated):
                        is_reply = 1 if idx == 2 and reply_targets.get(2) else 0
                        reply_url = None
                        if is_reply and reply_targets.get(2):
                            rt = reply_targets[2]
                            reply_url = f"https://www.reddit.com{rt.get('permalink', '')}" if rt.get('permalink') else None
                        mentions = 1 if brand_name.lower() in body.lower() else 0
                        cid = db2.add_search_comment(
                            search_post_id=pid, body=body, brand_id=post.get("brand_id"),
                            persona_id=generation.get("config", {}).get(f"persona_{idx+1}"),
                            is_reply=is_reply, reply_to_url=reply_url,
                            mentions_brand=mentions, relevance_score=rel_score
                        )
                        stored.append({"id": cid, "body": body[:100]})

                    db2.update_search_post_status(pid, "complete")
                    results.append({"pid": pid, "generated": len(stored), "relevance_score": rel_score})

                except Exception as e:
                    db2.update_search_post_status(pid, "saved")
                    results.append({"pid": pid, "error": str(e)})
                    print(f"[BATCH GEN ERROR] post {pid}: {e}", flush=True)

            return {"total": len(valid_posts), "results": results}
        finally:
            db2.close()

    tid = start_task("generate-search-comments-batch", task)
    return jsonify({"task_id": tid, "post_count": len(valid_posts)})


@app.route("/api/search/comments")
def api_list_search_comments():
    db = get_db()
    try:
        search_post_id = request.args.get("search_post_id", type=int)
        status = request.args.get("status")
        comments = db.list_search_comments(search_post_id=search_post_id, status=status)
        return jsonify(comments)
    finally:
        db.close()


@app.route("/api/search/comments/<int:cid>/assign", methods=["POST"])
def api_assign_search_comment(cid):
    db = get_db()
    try:
        account_id = request.json.get("account_id")
        if not account_id:
            return jsonify({"error": "account_id required"}), 400
        db.assign_search_comment(cid, account_id)
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/search/comments/<int:cid>/unassign", methods=["POST"])
def api_unassign_search_comment(cid):
    db = get_db()
    try:
        db.unassign_search_comment(cid)
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/search/comments/<int:cid>/inform", methods=["POST"])
def api_inform_search_comment(cid):
    db = get_db()
    try:
        db.inform_search_comment(cid)
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/search/comments/<int:cid>/deploy", methods=["POST"])
def api_deploy_search_comment(cid):
    db = get_db()
    try:
        url = request.json.get("reddit_comment_url", "")
        if not url:
            return jsonify({"error": "reddit_comment_url required"}), 400
        db.deploy_search_comment(cid, url)
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/search/comments/<int:cid>/body", methods=["PUT"])
def api_update_search_comment_body(cid):
    db = get_db()
    try:
        data = request.json
        body = data.get("body", "").strip()
        if not body:
            return jsonify({"error": "Body cannot be empty"}), 400
        db.update_search_comment_body(cid, body)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/search/comments/<int:cid>", methods=["DELETE"])
def api_delete_search_comment(cid):
    db = get_db()
    try:
        db.delete_search_comment(cid)
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/search/comments/<int:cid>/undeploy", methods=["POST"])
def api_undeploy_search_comment(cid):
    db = get_db()
    try:
        db.undeploy_search_comment(cid)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/search/comments/<int:cid>/mark-paid", methods=["POST"])
def api_mark_search_comment_paid(cid):
    db = get_db()
    try:
        db.mark_search_comment_paid(cid)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/search/comments/<int:cid>/mark-removed", methods=["POST"])
def api_mark_search_comment_removed(cid):
    db = get_db()
    try:
        db.mark_search_comment_removed(cid)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/search/comments/<int:cid>/unremove", methods=["POST"])
def api_unremove_search_comment(cid):
    db = get_db()
    try:
        db.unremove_search_comment(cid)
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/search/posts/<int:pid>/archive", methods=["POST"])
def api_archive_search_post(pid):
    db = get_db()
    try:
        db.archive_search_post(pid)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/search/posts/<int:pid>/unarchive", methods=["POST"])
def api_unarchive_search_post(pid):
    db = get_db()
    try:
        db.unarchive_search_post(pid)
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/accounts/search-assignment-status")
def api_accounts_search_assignment_status():
    db = get_db()
    try:
        return jsonify(db.get_accounts_with_search_assignment_counts())
    finally:
        db.close()


@app.route("/api/accounts/<username>/search-comments")
def api_account_search_comments(username):
    db = get_db()
    try:
        return jsonify(db.get_search_comments_for_account(username))
    finally:
        db.close()


@app.route("/api/due-payments")
def api_due_payments():
    db = get_db()
    try:
        return jsonify(db.get_due_payments())
    finally:
        db.close()


@app.route("/api/analytics/payments")
def api_analytics_payments():
    """Get deployed items with payment status, filterable."""
    db = get_db()
    try:
        return jsonify(db.get_payment_data(
            subreddit_id=request.args.get("subreddit_id", type=int),
            brand_id=request.args.get("brand_id", type=int),
            account_id=request.args.get("account_id") or None,
            paid_filter=request.args.get("paid_filter") or None,
            limit=request.args.get("limit", 100, type=int),
            offset=request.args.get("offset", 0, type=int),
        ))
    finally:
        db.close()


@app.route("/api/search/brands")
def api_search_brands():
    """List all brands (including standalone) for the search feature."""
    db = get_db()
    try:
        rows = db.conn.execute("""
            SELECT b.*, s.name as subreddit_name
            FROM brands b
            LEFT JOIN subreddits s ON b.subreddit_id = s.id
            ORDER BY b.name
        """).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@app.route("/api/search/brands", methods=["POST"])
def api_create_standalone_brand():
    """Create a brand without requiring a subreddit."""
    db = get_db()
    try:
        data = request.json or {}
        name = data.get("name", "").strip()
        if not name:
            return jsonify({"error": "name is required"}), 400
        context = data.get("context", "").strip() or name
        domain_url = data.get("domain_url", "").strip()
        keywords = json.dumps(data.get("keywords", []))
        cur = db.conn.execute(
            "INSERT INTO brands (subreddit_id, name, domain_url, context, keywords) VALUES (NULL, ?, ?, ?, ?)",
            (name, domain_url, context, keywords)
        )
        db.conn.commit()
        return jsonify({"id": cur.lastrowid, "name": name})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Debug: network diagnostic (remove after debugging)
# ---------------------------------------------------------------------------

@app.route("/api/debug/network")
def api_debug_network():
    """Test outbound HTTP from this server."""
    import requests as _requests
    results = {}

    # Test 1: Reddit direct (expected to 403 from cloud IPs)
    try:
        r = _requests.get(
            "https://www.reddit.com/user/spez/about.json",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=10,
        )
        results["reddit_direct"] = {"status": r.status_code, "body_preview": r.text[:200]}
    except Exception as e:
        results["reddit_direct"] = {"error": str(e)}

    # Test 1b: Reddit via proxy
    try:
        r = _reddit_get("/user/spez/about.json", timeout=10)
        results["reddit_proxy"] = {"status": r.status_code, "body_preview": r.text[:200]}
    except Exception as e:
        results["reddit_proxy"] = {"error": str(e)}

    # Test 2: Generic HTTPS
    try:
        r = _requests.get("https://httpbin.org/ip", timeout=10)
        results["httpbin"] = {"status": r.status_code, "body": r.text[:200]}
    except Exception as e:
        results["httpbin"] = {"error": str(e)}

    # Test 3: Website fetch
    try:
        r = _requests.get(
            "https://getpetermd.com/",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=10,
        )
        results["website"] = {"status": r.status_code, "body_len": len(r.text)}
    except Exception as e:
        results["website"] = {"error": str(e)}

    # Test 4: Anthropic API (just check connectivity, don't make a real call)
    try:
        r = _requests.get("https://api.anthropic.com/v1/models", timeout=10,
                          headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"})
        results["anthropic"] = {"status": r.status_code}
    except Exception as e:
        results["anthropic"] = {"error": str(e)}

    return jsonify(results)

# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def _fetch_reddit_user_data(username):
    """Fetch karma and account age from Reddit user API. Returns dict or None on failure."""
    try:
        resp = _reddit_get(f"/user/{username}/about.json")
        print(f"[REDDIT] u/{username} → status {resp.status_code}", flush=True)
        if resp.status_code == 200:
            try:
                body = resp.json()
            except Exception as je:
                return {"_error": f"Invalid JSON from Reddit: {je}"}
            data = (body or {}).get("data") or {}
            # Reddit returns {} / partial data for suspended / gated / NSFW-hidden
            # accounts. Missing created_utc is a strong signal the payload is not
            # a real profile — treat it as a soft error so the refresh path can
            # record the failure instead of silently writing 0/0 karma.
            if not data or data.get("created_utc") is None:
                preview = json.dumps(body)[:200] if body else "<empty>"
                return {"_error": f"Empty/degraded profile payload: {preview}"}
            return {
                "link_karma": int(data.get("link_karma") or 0),
                "comment_karma": int(data.get("comment_karma") or 0),
                "created_utc": data.get("created_utc"),
                "is_suspended": bool(data.get("is_suspended")),
            }
        else:
            print(f"[REDDIT] u/{username} response: {resp.text[:300]}", flush=True)
            return {"_error": f"Reddit returned status {resp.status_code}"}
    except Exception as e:
        print(f"[REDDIT] u/{username} error: {e}", flush=True)
        return {"_error": f"Request failed: {e}"}

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
        if reddit_data and "_error" not in reddit_data:
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

@app.route("/api/accounts/<username>/toggle-exclude", methods=["POST"])
def api_toggle_account_exclude(username):
    db = get_db()
    try:
        new_val = db.toggle_account_excluded(username)
        if new_val is None:
            return jsonify({"error": "Account not found"}), 404
        return jsonify({"ok": True, "excluded": new_val})
    finally:
        db.close()

@app.route("/api/accounts/<username>/refresh", methods=["POST"])
def api_refresh_account(username):
    def task():
        db2 = Database(DB_PATH)
        db2.connect()
        db2.initialize()
        try:
            reddit_data = _fetch_reddit_user_data(username)
            if not reddit_data or "_error" in reddit_data:
                err = reddit_data.get("_error", "Unknown") if reddit_data else "No response"
                # Persist the failure so the UI can surface it (instead of the
                # old silent-failure behavior that only advanced last_refreshed
                # on success).
                try:
                    db2.record_refresh_failure(username, err)
                except Exception:
                    pass
                raise Exception(f"Could not fetch data for u/{username}: {err}")
            db2.update_account_reddit_data(
                username,
                reddit_data["link_karma"],
                reddit_data["comment_karma"],
                reddit_data["created_utc"],
            )
            return {"ok": True, **reddit_data}
        finally:
            db2.close()

    tid = start_task("refresh-account", task)
    return jsonify({"task_id": tid})


@app.route("/api/accounts/refresh-stale", methods=["POST"])
def api_refresh_stale_accounts():
    """Bulk-refresh every account that looks stale or previously failed.
    Covers: never-refreshed, >7 days old, last_refresh_error set, or total
    karma < 10 (a strong signal of a prior silent failure)."""
    import time as _time

    def task():
        db2 = Database(DB_PATH)
        db2.connect()
        db2.initialize()
        try:
            usernames = db2.list_stale_accounts()
            refreshed = 0
            failed = 0
            errors = []
            for uname in usernames:
                data = _fetch_reddit_user_data(uname)
                if data and "_error" not in data:
                    db2.update_account_reddit_data(
                        uname, data["link_karma"], data["comment_karma"], data["created_utc"]
                    )
                    refreshed += 1
                else:
                    err = (data or {}).get("_error", "Unknown")
                    db2.record_refresh_failure(uname, err)
                    failed += 1
                    errors.append({"username": uname, "error": err})
                _time.sleep(1.5)  # gentle on the proxy / Reddit rate limits
            return {
                "ok": True,
                "total": len(usernames),
                "refreshed": refreshed,
                "failed": failed,
                "errors": errors[:50],
            }
        finally:
            db2.close()

    tid = start_task("refresh-stale-accounts", task)
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
                if reddit_data and "_error" not in reddit_data:
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
print(f"  REDDIT_PROXY_URL: {REDDIT_PROXY_URL or '(not set)'}")
print(f"  REDDIT_PROXY_URL (env): {os.environ.get('REDDIT_PROXY_URL', '(not in env)')}")
print("=" * 50)

# Ensure DB exists
db = get_db()
subs = db.list_subreddits()
total_posts = sum(s["post_count"] for s in subs)
total_comments = sum(s["comment_count"] for s in subs)
print(f"Database: {DB_PATH}")
print(f"  {len(subs)} subreddits | {total_posts} posts | {total_comments} comments")
db.cleanup_old_tasks(24)
print("  Cleaned up stale tasks")
db.close()

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    print(f"\nStarting web dashboard at http://localhost:{port}")
    app.run(debug=debug, port=port)
