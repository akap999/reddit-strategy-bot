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

# ---------------------------------------------------------------------------
# Strip standard forward-proxy env vars at startup.
#
# `requests` (and httpx, used by the Anthropic SDK) automatically route
# through HTTP_PROXY / HTTPS_PROXY / ALL_PROXY env vars if set. A leftover
# residential-proxy var (e.g. from an IPRoyal experiment) that's since
# gone stale makes EVERY outbound call return 407 Proxy Authentication
# Required — silently breaking Reddit RSS, Check Live, and even Claude.
#
# This app never needs a forward proxy for outbound traffic (the Reddit
# Cloudflare worker is a URL-rewrite proxy fetched directly, not a tunnel;
# Pullpush/Arctic/Claude are hit directly). So we proactively clear these
# vars so a stray one can't hijack the process. The residential proxy, if
# ever wanted, is honored explicitly via REDDIT_HTTP_PROXY + proxies=,
# never via these globals.
for _pv in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy"):
    if os.environ.pop(_pv, None):
        print(f"[startup] cleared forward-proxy env var {_pv} "
              f"(prevents 407 hijack of outbound calls)", flush=True)

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

# ---------------------------------------------------------------------------
# Guard against REDDIT_PROXY_URL being set to a FORWARD proxy by mistake.
#
# REDDIT_PROXY_URL must be a URL-rewrite proxy (the Cloudflare worker) —
# the code builds `{REDDIT_PROXY_URL}/r/<sub>/...` and fetches it directly.
# If someone pastes a forward/tunnel proxy here (e.g. a residential proxy
# like http://user:pass@host:port), every fetch becomes a malformed URL
# with embedded credentials hitting the proxy host as if it were a Reddit
# mirror → 407, AND the credentials leak into logs / debug output.
#
# Detect the forward-proxy shape (embedded `user:pass@` credentials) and
# refuse to use it: blank both the module global and the env var so the
# code falls back to direct Reddit instead of leaking creds / 407-ing.
# (The operator still needs to set the correct worker URL for RSS to work
# from a blocked cloud IP — this just prevents the broken/leaky state.)
if REDDIT_PROXY_URL and "@" in REDDIT_PROXY_URL:
    print("[startup] ⚠ REDDIT_PROXY_URL looks like a FORWARD proxy "
          "(contains embedded credentials). That variable must be your "
          "Cloudflare worker URL, not a residential/forward proxy. "
          "Ignoring it to avoid leaking credentials and 407 errors. "
          "Set REDDIT_PROXY_URL to your worker URL.", flush=True)
    REDDIT_PROXY_URL = ""
    os.environ.pop("REDDIT_PROXY_URL", None)

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
    # Client portal has its own auth (email + password); the admin
    # Google-OAuth gate doesn't apply to /portal/* routes.
    if request.path.startswith('/portal'):
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

def _reddit_http_proxies():
    """Return a requests-style proxies dict for the residential HTTP
    proxy (IPRoyal etc.) when REDDIT_HTTP_PROXY is set, else None.

    This is a FORWARD proxy (tunnel): requests go to the real
    reddit.com URL but egress through the residential IP. Unlike the
    Cloudflare worker (REDDIT_PROXY_URL, which rewrites the path),
    this lets us hit Reddit's JSON endpoints — which Reddit serves to
    residential IPs but blocks for datacenter IPs.
    """
    p = os.environ.get("REDDIT_HTTP_PROXY", "").strip()
    if not p:
        # Explicit no-proxy dict (NOT None). Passing this to requests
        # disables any HTTP_PROXY/HTTPS_PROXY env-var hijack for the
        # call. (None would let requests fall back to env proxies.)
        return {"http": None, "https": None}
    return {"http": p, "https": p}


def _comment_liveness_via_rss(comment_url, timeout=15):
    """Determine whether a specific Reddit comment is still live using
    its permalink RSS feed — the bypass for the JSON API being
    auth-gated for cloud IPs.

    How it works (verified empirically): requesting a comment's
    permalink RSS, `/r/<sub>/comments/<post>/comment/<cid>/.rss`,
    returns an <entry> for that comment when it's LIVE. When the
    comment is removed / deleted / nonexistent, Reddit omits it and
    serves the post (or parent) entries instead — so the comment id
    simply won't appear in the feed.

    Returns:
      'live'    — the comment id appears in its own permalink RSS
      'removed' — feed fetched OK but the comment id is absent
      None      — couldn't fetch / parse (caller should fall back or
                  treat as inconclusive, NOT as removed)

    Routes through REDDIT_PROXY_URL (the worker that reaches Reddit
    RSS) when set, else direct www.reddit.com.
    """
    import requests as _requests
    import xml.etree.ElementTree as _ET
    if not comment_url:
        return None
    # Parse /r/SUB/comments/POSTID/.../COMMENTID from the URL.
    m = re.search(r"/r/([^/]+)/comments/([a-z0-9]+)(?:/[^/]*)*?/([a-z0-9]{4,12})/?(?:\?|$)",
                  comment_url, re.IGNORECASE)
    if not m:
        # Fallback: simpler parse — post id after /comments/, comment
        # id = last path segment.
        mp = re.search(r"/r/([^/]+)/comments/([a-z0-9]+)", comment_url, re.IGNORECASE)
        seg = [s for s in comment_url.split("?")[0].rstrip("/").split("/") if s]
        if not mp or not seg:
            return None
        sub, post_id, cid = mp.group(1), mp.group(2), seg[-1]
    else:
        sub, post_id, cid = m.group(1), m.group(2), m.group(3)
    cid = cid.lower()
    # Guard: if the "comment id" we parsed equals the post id, the URL
    # was a post link, not a comment link — can't check a comment.
    if cid == post_id.lower():
        return None
    proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
    base = proxy.rstrip("/") if proxy else "https://www.reddit.com"
    rss_url = f"{base}/r/{sub}/comments/{post_id}/comment/{cid}/.rss"
    headers = {
        "User-Agent": REDDIT_USER_AGENT,
        "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.5",
    }
    try:
        r = _requests.get(rss_url, headers=headers, timeout=timeout,
                          proxies={"http": None, "https": None})
        if r.status_code != 200:
            return None
        body = r.text or ""
        if not body.lstrip().startswith("<?xml"):
            return None
        root = _ET.fromstring(body)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns):
            eid = (entry.find("atom:id", ns).text or "")
            if cid in eid.lower():
                return "live"
        # Feed parsed but our comment id isn't in it → removed/deleted.
        return "removed"
    except Exception as e:
        print(f"[COMMENT-RSS] liveness check failed for {comment_url}: {e}", flush=True)
        return None


def _post_liveness_via_rss(post_url, timeout=15):
    """Liveness check for a POST url via its comment RSS feed.

    /r/<sub>/comments/<postid>/.rss returns Atom entries when the post
    is live (the post itself + comments). A removed/deleted post
    returns an empty feed (no entries) or a feed whose post entry body
    is [removed]/[deleted].

    Returns 'live' / 'removed' / None(inconclusive).
    """
    import requests as _requests
    import xml.etree.ElementTree as _ET
    if not post_url:
        return None
    m = re.search(r"(/r/[^/]+/comments/[a-z0-9]+)", post_url, re.IGNORECASE)
    if not m:
        return None
    proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
    base = proxy.rstrip("/") if proxy else "https://www.reddit.com"
    rss_url = f"{base}{m.group(1)}/.rss"
    headers = {
        "User-Agent": REDDIT_USER_AGENT,
        "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.5",
    }
    try:
        r = _requests.get(rss_url, params={"limit": 5}, headers=headers, timeout=timeout,
                          proxies={"http": None, "https": None})
        if r.status_code == 404:
            return "removed"
        if r.status_code != 200 or not (r.text or "").lstrip().startswith("<?xml"):
            return None
        root = _ET.fromstring(r.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)
        if not entries:
            # Live posts return at least the post entry; an empty feed
            # is a strong removed signal.
            return "removed"
        # Live: feed has entries. (We don't try to detect [removed]
        # post bodies here — an empty feed is the reliable signal.)
        return "live"
    except Exception:
        return None


def _reddit_comment_meta_via_rss(comment_url, timeout=15):
    """Fetch a comment's metadata via its permalink RSS feed — the
    cloud-IP-safe replacement for the JSON API that bulk-deploy relies
    on (Reddit gates `.json` for datacenter IPs, so the JSON path returns
    nothing on Railway and every row falls through to 'missing').

    Returns the SAME dict shape as
    `bulk_deploy.fetch_reddit_comment_meta`:
        {"body","posted_at","is_removed","found","author"}

    found=False  → feed couldn't be fetched/parsed (inconclusive; the
                   caller treats this as 'missing' and may fall back to
                   the JSON fetcher for local dev).
    found=True + is_removed=False → comment is live; `body`/`posted_at`/
                   `author` populated from the Atom entry.
    found=True + is_removed=True  → feed parsed but the comment id is
                   absent (removed/deleted) — same signal Check Live uses.

    Routes through REDDIT_PROXY_URL (the worker) when set.
    """
    import requests as _requests
    import xml.etree.ElementTree as _ET
    from html import unescape as _unescape
    from datetime import datetime as _dt, timezone as _tz

    out = {"body": None, "posted_at": None, "is_removed": False,
           "found": False, "author": None}
    if not comment_url:
        return out

    m = re.search(r"/r/([^/]+)/comments/([a-z0-9]+)(?:/[^/]*)*?/([a-z0-9]{4,12})/?(?:\?|$)",
                  comment_url, re.IGNORECASE)
    if not m:
        mp = re.search(r"/r/([^/]+)/comments/([a-z0-9]+)", comment_url, re.IGNORECASE)
        seg = [s for s in comment_url.split("?")[0].rstrip("/").split("/") if s]
        if not mp or not seg:
            return out
        sub, post_id, cid = mp.group(1), mp.group(2), seg[-1]
    else:
        sub, post_id, cid = m.group(1), m.group(2), m.group(3)
    cid = cid.lower()
    if cid == post_id.lower():
        return out  # post link, not a comment link

    proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
    base = proxy.rstrip("/") if proxy else "https://www.reddit.com"
    rss_url = f"{base}/r/{sub}/comments/{post_id}/comment/{cid}/.rss"
    headers = {
        "User-Agent": REDDIT_USER_AGENT,
        "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.5",
    }

    def _strip_tags(html):
        txt = re.sub(r"<[^>]+>", " ", html or "")
        return re.sub(r"\s+", " ", _unescape(txt)).strip()

    def _pub_to_stored(s):
        if not s:
            return None
        try:
            d = _dt.fromisoformat(s.strip().replace("Z", "+00:00"))
            if d.tzinfo:
                d = d.astimezone(_tz.utc).replace(tzinfo=None)
            return d.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    try:
        r = _requests.get(rss_url, headers=headers, timeout=timeout,
                          proxies={"http": None, "https": None})
        if r.status_code != 200:
            return out
        text = r.text or ""
        if not text.lstrip().startswith("<?xml"):
            return out
        root = _ET.fromstring(text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns):
            eid = (entry.findtext("atom:id", default="", namespaces=ns) or "")
            if cid not in eid.lower():
                continue
            content = entry.findtext("atom:content", default="", namespaces=ns) or ""
            body = _strip_tags(content)
            author = entry.findtext("atom:author/atom:name", default="", namespaces=ns) or ""
            out["found"] = True
            out["body"] = body or None
            out["posted_at"] = _pub_to_stored(
                entry.findtext("atom:published", default="", namespaces=ns)
                or entry.findtext("atom:updated", default="", namespaces=ns))
            out["author"] = (author or "").strip().lstrip("/").replace("u/", "").strip() or None
            stripped = (body or "").strip().lower()
            out["is_removed"] = stripped in ("[removed]", "[deleted]")
            return out
        # Feed parsed but the comment id isn't in it → removed/deleted.
        out["found"] = True
        out["is_removed"] = True
        return out
    except Exception as e:
        print(f"[COMMENT-RSS-META] failed for {comment_url}: {e}", flush=True)
        return out


def _reddit_get(path, timeout=15, max_retries=3):
    """GET a Reddit API path. Three transport modes, in priority order:

    1. Residential HTTP proxy (REDDIT_HTTP_PROXY) — tunnels to
       www.reddit.com directly through a residential IP. Reddit serves
       JSON to residential IPs, so this is the preferred path when set.
    2. Cloudflare worker (REDDIT_PROXY_URL) — path-rewrite proxy; serves
       RSS but Reddit 403s its JSON.
    3. Direct old.reddit.com — last resort.

    path should start with / e.g. /user/spez/about.json
    Retries on transient errors (403/429/5xx, HTML-instead-of-JSON).
    """
    import requests as _requests
    import time as _time
    http_proxies = _reddit_http_proxies()
    proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
    if http_proxies:
        # Residential proxy: hit www.reddit.com directly, tunneled.
        base = "https://www.reddit.com"
    else:
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
            print(f"[REDDIT_GET] {url} (transport={'residential' if http_proxies else ('worker' if proxy else 'direct')}, attempt={attempt+1})", flush=True)
            resp = _requests.get(url, headers=headers, timeout=timeout,
                                 proxies=http_proxies)
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

    # If the chosen transport returned HTML after all retries, try
    # old.reddit.com as a fallback (routed through the residential
    # proxy too when set). old.reddit.com has lighter bot-blocking and
    # often returns JSON even when www.reddit.com serves HTML/403.
    if (proxy or http_proxies) and last_resp and last_resp.status_code == 200 and last_resp.text.lstrip()[:1] == "<":
        fallback_url = f"https://old.reddit.com{path}"
        print(f"[REDDIT_GET] Got HTML after retries, trying old.reddit.com fallback: {fallback_url}", flush=True)
        try:
            fb_resp = _requests.get(fallback_url, headers=headers, timeout=timeout,
                                    proxies=http_proxies)
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

def start_task(task_type, func, *args, pass_task_id=False, **kwargs):
    task_id = str(uuid.uuid4())
    db = get_db()
    db.create_task(task_id, task_type)
    db.close()
    if pass_task_id:
        kwargs["_task_id"] = task_id
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

@app.route("/api/analytics/live-status")
def api_live_status_analytics():
    db = get_db()
    try:
        return jsonify(db.get_live_status_analytics())
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

def _parse_live_param():
    """Parse ?live=0|1|all into the value db helpers expect.
    None = both (rare, mostly for admin/debug)
    False = regular only (default — existing pages behave as before)
    True = live only (Live Subreddits page calls with this)
    """
    raw = request.args.get("live")
    if raw is None or raw == "":
        return False
    if raw in ("1", "true", "yes"):
        return True
    if raw == "all":
        return None
    return False


@app.route("/api/subreddits")
def api_list_subreddits():
    db = get_db()
    try:
        return jsonify(db.list_subreddits(live=_parse_live_param()))
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
    # `use_cases` / `pain_points` / `features` / `competitors` /
    # `search_subreddits` are plain JSON list-of-strings.
    for listf in ("use_cases", "pain_points", "features", "competitors", "search_subreddits"):
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
    # `focus` is a JSON list of {phrase, applies_when} dicts. Two input
    # syntaxes are accepted from the textarea:
    #   1. plain phrase per line:        `fiberglass-free`
    #      → {phrase: "fiberglass-free", applies_when: []}  (auto-detect)
    #   2. phrase + manual scope keywords:
    #      `fiberglass-free | mattress, foam`
    #      → {phrase: "fiberglass-free", applies_when: ["mattress","foam"]}
    # API callers can also pass an explicit JSON list of dicts, which is
    # forwarded as-is after normalisation. Plain string entries inside a
    # list are coerced to {phrase: s, applies_when: []} for backwards
    # compatibility with the old `[str]` shape.
    if "focus" in data:
        v = data.get("focus")
        if v is None:
            out["focus"] = None
        else:
            entries = []
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        phrase = str(item.get("phrase", "")).strip()
                        if not phrase:
                            continue
                        aw = item.get("applies_when") or []
                        if not isinstance(aw, list):
                            aw = []
                        aw = [str(x).strip() for x in aw if str(x).strip()]
                        entries.append({"phrase": phrase, "applies_when": aw})
                    elif isinstance(item, str) and item.strip():
                        e = _parse_focus_line(item)
                        if e:
                            entries.append(e)
            elif isinstance(v, str):
                # Split on newlines only — commas are now part of the
                # applies_when keyword list (e.g. "X | a, b"). Each line
                # becomes one phrase entry.
                for line in v.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    e = _parse_focus_line(line)
                    if e:
                        entries.append(e)
            # De-duplicate by phrase (case-insensitive), keep last.
            dedup = {}
            for e in entries:
                if e and e.get("phrase"):
                    dedup[e["phrase"].lower()] = e
            out["focus"] = json.dumps(list(dedup.values()))
    if "enriched_at" in data:
        out["enriched_at"] = data.get("enriched_at")
    return out


def _parse_focus_line(line):
    """Parse one focus textarea line into a {phrase, applies_when} dict.

    Accepts:
      "fiberglass-free"                  → applies_when=[]
      "fiberglass-free | mattress, foam" → applies_when=["mattress","foam"]
      "fiberglass-free|mattress|foam"    → applies_when=["mattress","foam"]
    Returns None if the phrase part is empty.
    """
    if not isinstance(line, str):
        return None
    parts = [p.strip() for p in line.split("|")]
    phrase = parts[0] if parts else ""
    if not phrase:
        return None
    applies_when = []
    if len(parts) > 1:
        # Anything after the first `|` is treated as keyword scope.
        # Join the remaining segments with commas so both
        # `phrase | a, b` and `phrase | a | b` shapes work.
        tail = ",".join(parts[1:])
        applies_when = [s.strip() for s in tail.split(",") if s.strip()]
    return {"phrase": phrase, "applies_when": applies_when}

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


@app.route("/api/brands/<int:bid>/personas/regenerate", methods=["POST"])
def api_regenerate_brand_personas(bid):
    """Clear + regenerate this brand's auto personas now (grounded in its site +
    enrichment). Returns the new personas. Manual context is never touched."""
    from generators.brand_enrichment import generate_brand_personas
    db = get_db()
    try:
        brand = db.get_brand(bid)
        if not brand:
            return jsonify({"error": "brand not found"}), 404
        claude = ClaudeClient(ANTHROPIC_API_KEY)
        personas = generate_brand_personas(
            claude, brand["name"], brand.get("domain_url") or "",
            category=brand.get("category") or "", audience=brand.get("audience") or "",
            use_cases=brand.get("use_cases"), pain_points=brand.get("pain_points"))
        db.update_brand(bid, personas=json.dumps(personas))
        return jsonify({"personas": personas})
    finally:
        db.close()


@app.route("/api/brands/<int:bid>/focus-coverage")
def api_brand_focus_coverage(bid):
    """Aggregate focus-pairing hit/miss counts for a brand.

    Reads `comments.focus_phrase` and `comments.focus_hit` (set on save by
    the comment generator when a focus-phrase pairing was attempted) and
    returns a per-phrase breakdown:

        {
          "fiberglass-free": {"assigned": 18, "hit": 16, "miss": 2},
          "toxic-free":      {"assigned": 6,  "hit": 4,  "miss": 2}
        }

    Surfaces the focus strategy's reliability per phrase so the user can
    spot phrases that are systematically missing the brand-pairing target.
    """
    db = get_db()
    try:
        brand = db.get_brand(bid)
        if not brand:
            return jsonify({"error": "brand not found"}), 404
        rows = db.conn.execute(
            """SELECT focus_phrase, focus_hit, COUNT(*) AS n
               FROM comments
               WHERE brand_id = ? AND focus_phrase IS NOT NULL
               GROUP BY focus_phrase, focus_hit""",
            (bid,),
        ).fetchall()
    finally:
        db.close()

    coverage = {}
    for r in rows:
        phrase = r["focus_phrase"]
        hit = r["focus_hit"]
        n = r["n"] or 0
        bucket = coverage.setdefault(phrase, {"assigned": 0, "hit": 0, "miss": 0})
        bucket["assigned"] += n
        if hit == 1:
            bucket["hit"] += n
        else:
            # focus_hit = 0 (assigned-but-missed) or NULL on this row
            # (shouldn't happen given the WHERE clause, but treat as miss
            # for safety).
            bucket["miss"] += n
    return jsonify(coverage)

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
            live=_parse_live_param(),
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
        # Subreddit name for the post-detail modal's r/<sub> ↗ link.
        # `posts.subreddit_id` is the FK; the frontend wants the name.
        try:
            sid = post.get("subreddit_id")
            if sid:
                sr = db.conn.execute(
                    "SELECT name FROM subreddits WHERE id = ?", (sid,)
                ).fetchone()
                post["subreddit_name"] = sr["name"] if sr else None
        except Exception:
            post["subreddit_name"] = None
        brands = db.get_brands_for_post(pid)
        post["brands"] = [{"id": b["id"], "name": b["name"]} for b in brands]
        post["brand_names"] = ", ".join(b["name"] for b in brands) if brands else ""
        return jsonify(post)
    finally:
        db.close()

@app.route("/api/posts/<int:pid>/regenerate-body", methods=["POST"])
def api_regenerate_post_body(pid):
    """Rewrite a post's BODY for the SAME title (title never changes). Synchronous —
    one Claude call. Grounds the new body in the post's brand(s) + its ai_search_meta
    (anchor/target_query/persona) and the shared ask-once body rules."""
    db, claude, _, post_gen, _ = make_generators()
    try:
        post = db.get_post(pid)
        if not post:
            return jsonify({"error": "Not found"}), 404
        brands = db.get_brands_for_post(pid)
        if not brands:
            return jsonify({"error": "post has no linked brand to ground the body"}), 400
        body = post_gen.regenerate_body(post, brands)
        if not body:
            return jsonify({"error": "regeneration returned no body"}), 502
        db.update_post_body(pid, body)
        return jsonify({"body": body})
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
        # If the post is currently in a monthly report, exit report cleanly
        # FIRST — this reverts its reported HQ anchor comment back to
        # 'deployed' so the anchor isn't stranded in 'report' (which would
        # make the report modal wrongly say "no HQ comment will anchor"),
        # and clears report_month/report_added_at. (Normally Deploy isn't
        # offered on a report post, but guard the API path regardless.)
        try:
            _cur = db.conn.execute("SELECT status FROM posts WHERE id = ?", (pid,)).fetchone()
            if _cur and _cur["status"] == "report":
                db.undo_post_report(pid, actor_email=_admin_email())
        except Exception as e:
            print(f"[publish] report-exit guard failed pid={pid}: {e}", flush=True)
        db.update_post_status(pid, "published")
        # Set deployed_at + clear any lingering report/undo breadcrumbs so a
        # live published post never carries stale report_month / prev_status.
        from datetime import datetime as _dt
        db.conn.execute(
            "UPDATE posts SET deployed_at = ?, report_month = NULL, "
            "report_added_at = NULL, prev_status = NULL WHERE id = ?",
            (_dt.now().strftime("%Y-%m-%d %H:%M:%S"), pid)
        )
        db.conn.commit()
        post = db.get_post(pid)
        if reddit_url and post:
            db.link_url_to_post(pid, reddit_url, post["subreddit_id"])
            # Piggy-back the Reddit-side publish timestamp so the
            # client dashboard's "Published Date" reflects when the
            # post actually went live on Reddit, not when the admin
            # clicked Mark Published. Best-effort + cached; the API
            # call already proxies through Cloudflare so it's cheap.
            try:
                _spawn_post_posted_at_fetch(pid, reddit_url)
            except Exception as e:
                print(f"[publish] posted_at fetch spawn failed pid={pid}: {e}", flush=True)
        if owner_account:
            db.set_post_owner(pid, owner_account)
        return jsonify({"ok": True})
    finally:
        db.close()


def _fetch_post_posted_at(reddit_url):
    """Resolve a Reddit post URL to its `created_utc` (ISO string).
    Returns None on any failure — caller treats that as "leave NULL,
    use deployed_at fallback".
    """
    if not reddit_url:
        return None
    from urllib.parse import urlparse
    from bulk_deploy import _utc_seconds_to_iso
    try:
        parsed = urlparse(reddit_url.split("?")[0].rstrip("/"))
        path = parsed.path.rstrip("/") + ".json"
    except Exception:
        return None
    data = _reddit_get_json(path)
    if not data or not isinstance(data, list) or len(data) < 1:
        return None
    children = (data[0] or {}).get("data", {}).get("children", [])
    if not children:
        return None
    cu = (children[0] or {}).get("data", {}).get("created_utc")
    return _utc_seconds_to_iso(cu)


def _spawn_post_posted_at_fetch(pid, reddit_url):
    """Fire-and-forget background fetch + persist of a post's
    Reddit-side `created_utc`. Same pattern as the comment
    auto-stats hook on report — runs after the response is sent.
    """
    def task():
        bg = Database(DB_PATH)
        bg.connect(); bg.initialize()
        try:
            iso = _fetch_post_posted_at(reddit_url)
            if iso:
                bg.set_post_posted_at(pid, iso)
            return {"pid": pid, "posted_at": iso}
        finally:
            try: bg.close()
            except Exception: pass
    try:
        start_task("post-posted-at", task)
    except Exception as e:
        print(f"[posts.posted_at] spawn failed pid={pid}: {e}", flush=True)

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

@app.route("/api/comments/<int:cid>/reassign", methods=["POST"])
def api_reassign_comment(cid):
    """Change account_id on an already-assigned/informed comment without
    resetting status, assigned_at, informed_at, or prev_status."""
    db = get_db()
    try:
        data = request.json or {}
        new_account = (data.get("account_id") or "").strip()
        if not new_account:
            return jsonify({"error": "account_id is required"}), 400
        db.reassign_comment(cid, new_account)
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


@app.route("/api/comments/<int:cid>/regenerate", methods=["POST"])
def api_regenerate_comment(cid):
    """Re-roll a single comment in place. Synchronous (one Claude call,
    ~10-30s) — caller shows a spinner. Returns the new body so the
    frontend can patch the row without a refetch.

    Body: { ai_crawl?: bool, mention_brand?: bool }
    """
    from generators.comment_gen import CommentGenerator
    from generators.base import ClaudeClient
    data = request.json or {}
    ai_crawl = bool(data.get("ai_crawl", True))

    db = get_db()
    try:
        comment = db.get_comment(cid)
        if not comment:
            return jsonify({"error": "Comment not found"}), 404
        post = db.get_post(comment["post_id"])
        if not post:
            return jsonify({"error": "Post not found"}), 404
        # Default mention_brand to whatever the comment already is (preserves
        # the comment's original role in the thread) unless caller overrides.
        mention_brand = bool(data.get("mention_brand",
                                      bool(comment.get("mentions_brand"))))
        brand = None
        if comment.get("brand_id"):
            brand = db.get_brand(comment["brand_id"])
        if not brand:
            brands = db.get_brands_for_post(post["id"])
            brand = brands[0] if brands else None
        if not brand:
            return jsonify({"error": "No brand found for this comment"}), 400
        subreddit = db.get_subreddit(post["subreddit_id"])

        api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
        cg = CommentGenerator(ClaudeClient(api_key), db)

        # Mock tone — the original comment's tone analysis isn't stored.
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
        mock_stats = {"avg_chars": 300, "avg_words": 60, "median_chars": 250,
                      "min_chars": 50, "max_chars": 600}

        # If this is a reply, pass the parent body via reply_targets so the
        # regenerated comment still engages with the thread.
        reply_targets = None
        if comment.get("parent_comment_id"):
            parent = db.get_comment(comment["parent_comment_id"])
            if parent:
                reply_targets = {0: {
                    "body": parent["body"], "score": 5,
                    "author": parent.get("account_id") or "community_member",
                    "id": "", "permalink": "",
                }}

        result = cg.generate_comments(
            post_title=post["title"],
            post_body=post.get("body", ""),
            subreddit=subreddit["name"] if subreddit else "",
            comments=[],
            brand_name=brand["name"],
            brand_context=brand.get("context", ""),
            num_comments=1,
            tone_analysis=mock_tone,
            comment_stats=mock_stats,
            mention_brand_flags=[mention_brand],
            reply_targets=reply_targets,
            relevance={"best_angle": "regenerate this comment with a fresh take",
                       "natural_fit": 2},
            brand_assignments=[brand if mention_brand else None],
            all_brand_names=[brand["name"]],
            ai_crawl=ai_crawl,
            post_intent=post.get("intent"),
            brand_focus=cg._extract_brand_focus(brand),
        )
        bodies = result.get("generated_comments") or []
        if not bodies:
            return jsonify({"error": "Regeneration returned no body"}), 500
        new_body = bodies[0]
        db.update_comment_body(cid, new_body)
        return jsonify({"ok": True, "body": new_body})
    finally:
        db.close()


@app.route("/api/search/comments/<int:cid>/regenerate", methods=["POST"])
def api_regenerate_search_comment(cid):
    """Re-roll a single search comment in place. Synchronous.

    Body: { ai_crawl?: bool }
    """
    from generators.comment_gen import CommentGenerator
    from generators.base import ClaudeClient
    data = request.json or {}
    # Pre-consolidation parity: default ai_crawl off for Live Search.
    ai_crawl = bool(data.get("ai_crawl", False))

    db = get_db()
    try:
        row = db.conn.execute(
            """SELECT sc.*, sp.title as post_title, sp.reddit_url,
                      sp.subreddit as post_subreddit, sp.brand_id as post_brand_id
               FROM search_comments sc
               JOIN search_posts sp ON sc.search_post_id = sp.id
               WHERE sc.id = ?""", (cid,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Search comment not found"}), 404
        comment = dict(row)
        brand_id = comment.get("brand_id") or comment.get("post_brand_id")
        brand = db.get_brand(brand_id) if brand_id else None
        if not brand:
            return jsonify({"error": "No brand found for this comment"}), 400

        api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
        proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
        cg = CommentGenerator(
            ClaudeClient(api_key), db,
            reddit_base=proxy.rstrip("/") if proxy else None,
        )

        # Re-fetch live Reddit context so the regenerated comment is grounded
        # in the same conversation as the original generation.
        try:
            live_comments, post_body, _archived = cg.fetch_comments(
                comment["reddit_url"], limit=20)
        except Exception:
            live_comments, post_body = [], ""
        comment_stats = cg._compute_comment_stats(live_comments) if live_comments else {
            "avg_chars": 300, "avg_words": 60, "median_chars": 250,
            "min_chars": 50, "max_chars": 600,
        }
        # Best-effort tone — fall back to a casual default if the post has
        # no live comments to analyse.
        try:
            tone_analysis = cg.analyze_tone(
                comment["post_title"], post_body, comment["post_subreddit"],
                live_comments, comment_stats)
        except Exception:
            tone_analysis = None

        result = cg.generate_comments(
            post_title=comment["post_title"],
            post_body=post_body,
            subreddit=comment["post_subreddit"],
            comments=live_comments,
            brand_name=brand["name"],
            brand_context=brand.get("context", ""),
            num_comments=1,
            tone_analysis=tone_analysis,
            comment_stats=comment_stats,
            mention_brand_flags=[True],  # Live Search always brand-mentions
            all_brand_names=[brand["name"]],
            ai_crawl=ai_crawl,
            brand_focus=cg._extract_brand_focus(brand),
        )
        bodies = result.get("generated_comments") or []
        if not bodies:
            return jsonify({"error": "Regeneration returned no body"}), 500
        new_body = bodies[0]
        db.update_search_comment_body(cid, new_body)
        return jsonify({"ok": True, "body": new_body})
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
        url = data.get("reddit_comment_url", "")
        db.deploy_comment(
            cid,
            url,
            data.get("deployed_at"),
        )
    finally:
        db.close()
    # Kick off an async fetch to back-patch the real Reddit-posted
    # timestamp (Reddit's created_utc). The deploy already returned;
    # the user doesn't wait on Reddit availability. If the fetch fails
    # we leave posted_at NULL — the backfill button can pick it up later.
    if url:
        threading.Thread(
            target=_async_backpatch_posted_at,
            args=(cid, "comment", url),
            daemon=True,
        ).start()
    return jsonify({"ok": True})


def _async_backpatch_posted_at(comment_id, kind, comment_url):
    """Worker thread: fetch the Reddit comment's created_utc and store
    it on the row. Best-effort; failures are swallowed (a separate
    backfill job picks up missing rows on demand).
    """
    try:
        from bulk_deploy import fetch_reddit_comment_meta
    except Exception:
        return
    try:
        meta = fetch_reddit_comment_meta(comment_url, reddit_get=_reddit_get_json)
        posted_at = meta.get("posted_at")
        if not posted_at:
            return
        db = Database(DB_PATH)
        db.connect()
        try:
            db.update_posted_at(comment_id, kind, posted_at)
        finally:
            db.close()
    except Exception as e:
        print(f"[posted_at backpatch] cid={comment_id} kind={kind}: {e}", flush=True)


def _reddit_get_json(path, timeout=15, max_retries=3):
    """Thin wrapper around `_reddit_get` that decodes JSON. `_reddit_get`
    returns a raw Response object — every existing caller calls `.json()`
    on it. The bulk-deploy / backfill modules want the parsed JSON
    directly, so we centralise the decode here.
    """
    try:
        resp = _reddit_get(path, timeout=timeout, max_retries=max_retries)
    except Exception:
        return None
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception:
        return None


def _resolve_reddit_share_url_proxy(short_url):
    """Module-level Reddit /s/ share-link resolver, proxy-aware.

    Bulk Deploy passes this into `run_bulk_deploy` so /s/ links from
    the sheet get expanded to their canonical /comments/<post_id>/
    <slug>/<comment_id> form before being classified. Without this,
    every /s/ row in the sheet silently dropped to "no_match".
    """
    import requests as _requests
    import re as _re
    if not short_url or "/s/" not in short_url:
        return short_url
    proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
    s_path = _re.sub(r'^https?://[^/]+', '', short_url)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    # Proxy path first — the Cloudflare proxy exposes /resolve<path>
    # that follows the redirect server-side (faster + avoids residential
    # / cloud egress restrictions).
    if proxy:
        try:
            r = _requests.get(
                f"{proxy.rstrip('/')}/resolve{s_path}", timeout=20,
            )
            resolved = ((r.json() or {}).get("url") or "").split("?")[0].rstrip("/")
            if "/comments/" in resolved:
                return resolved
        except Exception:
            pass
    # Direct HEAD then GET fallback.
    for method in (_requests.head, _requests.get):
        try:
            r = method(short_url, headers=headers,
                       allow_redirects=True, timeout=20)
            resolved = (r.url or "").split("?")[0].rstrip("/")
            if "/comments/" in resolved:
                return resolved
        except Exception:
            continue
    return None

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
        restored = db.unremove_comment(cid)
        return jsonify({"ok": True, "status": restored or "deployed"})
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

@app.route("/api/comments/<int:cid>/unmark-paid", methods=["POST"])
def api_unmark_comment_paid(cid):
    """Revert a paid comment back to deployed (clears paid_at). Reliable
    even when prev_status was cleared by an earlier flow."""
    db = get_db()
    try:
        restored = db.unmark_comment_paid(cid)
        if not restored:
            return jsonify({"error": "comment is not in paid state"}), 422
        return jsonify({"ok": True, "status": restored})
    finally:
        db.close()

@app.route("/api/comments/<int:cid>/undo", methods=["POST"])
def api_undo_comment(cid):
    db = get_db()
    try:
        prev = db.undo_comment_status(cid)
        if prev is None:
            return jsonify({"error": "No previous status to undo"}), 400
        return jsonify({"ok": True, "new_status": prev})
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


@app.route("/api/posts/<int:pid>/mark-removed", methods=["POST"])
def api_mark_post_removed(pid):
    """Flip a post to status='removed'. Preserves the prior status
    in `prev_status` so the user can Undo. Idempotent — repeated
    calls are no-ops once the row is already removed.
    """
    db = get_db()
    try:
        row = db.conn.execute(
            "SELECT status FROM posts WHERE id = ?", (pid,)
        ).fetchone()
        if not row:
            return jsonify({"error": "not_found"}), 404
        if row["status"] == "removed":
            return jsonify({"ok": True, "noop": True})
        # `prev_status` already exists on the posts table.
        db.conn.execute(
            "UPDATE posts SET status = 'removed', prev_status = ? WHERE id = ?",
            (row["status"], pid)
        )
        db.conn.commit()
        return jsonify({"ok": True, "prev_status": row["status"]})
    finally:
        db.close()


@app.route("/api/posts/<int:pid>/undo-removed", methods=["POST"])
def api_undo_post_removed(pid):
    """Revert a 'removed' post back to its prev_status (default
    'complete' if missing). Lets the admin un-do a misclick.
    """
    db = get_db()
    try:
        row = db.conn.execute(
            "SELECT status, prev_status FROM posts WHERE id = ?", (pid,)
        ).fetchone()
        if not row:
            return jsonify({"error": "not_found"}), 404
        if row["status"] != "removed":
            return jsonify({"error": "not_in_removed_state"}), 422
        restore_to = row["prev_status"] or "complete"
        db.conn.execute(
            "UPDATE posts SET status = ?, prev_status = NULL WHERE id = ?",
            (restore_to, pid)
        )
        db.conn.commit()
        return jsonify({"ok": True, "restored_to": restore_to})
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
            bid, status=status or None, sort_by=sort_by or None,
            live=_parse_live_param(),
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
            live=_parse_live_param(),
        )
        return jsonify(result)
    finally:
        db.close()


def _check_live_batch(deployed, db, log_prefix="CHECK-LIVE", task_id=None):
    """Shared live-check logic for a batch of comments.
    Each item must have: id, reddit_comment_url, source ('comment' or 'search_comment').

    `task_id` (optional) — when provided, the function writes
    incremental progress to the `tasks.progress` column after each
    item is processed. The portal's polling loop reads this so the
    'Update status & stats' UI can show 'Processing X of Y…' as
    work progresses instead of hanging on a single 'Refreshing…'.

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

    restored = 0
    changes = []

    total = len(deployed)

    def _emit_progress():
        """Best-effort progress write. Failure is silent — the live-
        check work itself is more important than the progress UI."""
        if not task_id:
            return
        try:
            db.update_task_progress(task_id, {
                "checked": checked, "total": total,
                "live": live, "dead": dead,
                "restored": restored, "errors": errors,
            })
        except Exception as e:
            print(f"[{log_prefix}] progress update failed: {e}", flush=True)

    # Initial progress so the UI shows '0 of N' immediately.
    _emit_progress()

    def _backfill_posted_at_if_missing(item):
        """If the comment row's posted_at is NULL but we have a Reddit
        URL, fetch metadata once and patch the row before we decide
        between 'replace' and 'removed'. Works for both the Live Subs
        `comments` table and the Live Search `search_comments` table.
        Best-effort: any failure leaves posted_at as-is (the chooser
        then defaults to 'removed', which is the safe fallback).
        """
        url = (item.get("reddit_comment_url") or "").strip()
        src = item.get("source", "comment")
        if not url or src not in ("comment", "search_comment"):
            return
        table = "comments" if src == "comment" else "search_comments"
        try:
            row = db.conn.execute(
                f"SELECT posted_at FROM {table} WHERE id = ?",
                (item["id"],)
            ).fetchone()
            if row and row["posted_at"]:
                return
        except Exception:
            pass
        try:
            from bulk_deploy import fetch_reddit_comment_meta
            meta = fetch_reddit_comment_meta(url, reddit_get=_reddit_get_json)
            posted_at = meta.get("posted_at") if meta else None
            if posted_at:
                db.update_posted_at(item["id"], src, posted_at)
        except Exception as e:
            print(f"[{log_prefix}] posted_at backfill failed for "
                  f"#{item['id']} ({src}): {e}", flush=True)

    def _mark_dead(item, posted_at_hint=None):
        """Flip a comment to 'removed' or 'replace' via the 14-day
        chooser. `posted_at_hint` is the Reddit-side publish timestamp
        ("YYYY-MM-DD HH:MM:SS" UTC) extracted from the JSON we already
        fetched at the detection site — passing it through here avoids
        a second Reddit round trip that can fail under rate-limit and
        prevents the chooser from defaulting to 'removed' just because
        we never persisted posted_at.
        """
        src = item.get("source", "comment")
        prev = item.get("status", "")
        print(f"[{log_prefix}] _mark_dead #{item['id']} ({src}) prev_status={prev} hint={posted_at_hint!r}", flush=True)
        new_status = "removed"
        if src == "comment":
            # Reported comments that vanish on Reddit should land in
            # 'removed' or 'replace' (with report_month preserved) so
            # the client dashboard still shows them — just under the
            # "Removed" / "Replace" chip instead of "Live". The chooser
            # inside mark_comment_removed_or_replace decides which
            # based on the 14-day window.
            if prev == "report":
                if not posted_at_hint:
                    _backfill_posted_at_if_missing(item)
                chosen = db.mark_comment_removed_or_replace(
                    item["id"], posted_at_hint=posted_at_hint)
                new_status = chosen or "removed"
            else:
                # Non-reported deployed/paid/etc. comments: same rule
                # — auto-detected removal within 14 days → 'replace'.
                if (item.get("reddit_comment_url") or "").strip():
                    if not posted_at_hint:
                        _backfill_posted_at_if_missing(item)
                    chosen = db.mark_comment_removed_or_replace(
                        item["id"], posted_at_hint=posted_at_hint)
                    new_status = chosen or "removed"
                else:
                    # No Reddit URL — legacy mark_comment_deleted
                    # semantic for truly-local cleanup of never-
                    # deployed rows.
                    db.mark_comment_deleted(item["id"])
                    new_status = "deleted"
        else:
            # search_comment: same 14-day 'replace' rule.
            if (item.get("reddit_comment_url") or "").strip():
                if not posted_at_hint:
                    _backfill_posted_at_if_missing(item)
                chosen = db.mark_search_comment_removed_or_replace(
                    item["id"], posted_at_hint=posted_at_hint)
                new_status = chosen or "removed"
            else:
                db.mark_search_comment_removed(item["id"])
                new_status = "removed"
        db.log_live_check(item["id"], src, item.get("reddit_comment_url", ""),
                          "marked_dead", prev, new_status,
                          item.get("account_id"), item.get("subreddit"), item.get("brand_name"))
        changes.append({"id": item["id"], "source": src, "url": item.get("reddit_comment_url", ""),
                        "action": "marked_dead", "prev_status": prev, "new_status": new_status})

    def _mark_live(item):
        nonlocal restored
        src = item.get("source", "comment")
        cur_status = item.get("status", "")
        # If comment was removed/replace/deleted but is actually live → restore to deployed.
        # 'replace' is the recent-removal sub-state — if Reddit now
        # shows the comment alive (e.g. mod approved, OP undeleted),
        # treat it identically to a restored 'removed' row.
        if cur_status in ("removed", "replace", "deleted"):
            if src == "comment":
                db.restore_comment_to_deployed(item["id"])
            else:
                db.restore_search_comment_to_deployed(item["id"])
            restored += 1
            db.log_live_check(item["id"], src, item.get("reddit_comment_url", ""),
                              "restored", cur_status, "deployed",
                              item.get("account_id"), item.get("subreddit"), item.get("brand_name"))
            changes.append({"id": item["id"], "source": src, "url": item.get("reddit_comment_url", ""),
                            "action": "restored", "prev_status": cur_status, "new_status": "deployed"})
            print(f"[{log_prefix}] #{item['id']} ({src}) RESTORED to deployed (was {cur_status})", flush=True)
        else:
            if src == "comment":
                db.set_comment_live_check(item["id"])
            else:
                db.set_search_comment_live_check(item["id"])

    # =====================================================================
    # Bulk pre-pass for Live Subs comments. Many HQ threads have 4-8
    # comments under a single post; the per-item loop below would issue
    # one Reddit fetch per comment (= 4-8x more network + 3s pacing
    # between each = minutes for a moderate batch). The post's
    # /comments/<id>.json endpoint already returns the entire comment
    # tree in one response, so we can mark every Live Subs comment under
    # a given post from a single fetch.
    #
    # Items handled in this pre-pass are added to `handled_ids` and
    # skipped in the per-item loop below. Search comments (which have
    # no parent post container in this app's data model) and /s/ short
    # URLs (which need resolving before classification) stay on the
    # per-item path.
    # =====================================================================
    handled_ids = set()
    import re as _re_grp
    from urllib.parse import urlparse as _urlparse_grp

    def _parse_comment_url(u):
        """Return (post_url, comment_id) or (None, None)."""
        if not u or "/s/" in u:
            return (None, None)
        u_clean = u.strip().split("?")[0].rstrip("/")
        m = _re_grp.search(
            r"(/r/[^/]+/comments/[^/]+(?:/[^/]+)?)(?:/([A-Za-z0-9]{4,12}))?$",
            u_clean,
        )
        if not m:
            return (None, None)
        # Reconstruct the post URL on the same host as the original.
        post_path = m.group(1)
        comment_id = (m.group(2) or "").lower()
        try:
            parsed = _urlparse_grp(u_clean)
            host = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else "https://www.reddit.com"
        except Exception:
            host = "https://www.reddit.com"
        return (f"{host}{post_path}", comment_id)

    # Group Live Subs items by parent post URL.
    post_groups = {}  # post_url → [(item, comment_id)]
    for item in deployed:
        if item.get("source", "comment") != "comment":
            continue
        raw_url = (item.get("reddit_comment_url") or "").strip()
        if not raw_url:
            continue
        post_url, comment_id = _parse_comment_url(raw_url)
        if not post_url or not comment_id:
            continue
        post_groups.setdefault(post_url, []).append((item, comment_id))

    # Only bother with the bulk path when grouping actually saves
    # fetches (≥2 items share a post). Singletons still go through
    # the per-item loop — same network cost, simpler code path.
    def _walk_comment_tree(node, out):
        if isinstance(node, dict):
            kind = node.get("kind")
            if kind == "Listing":
                for child in (node.get("data") or {}).get("children", []) or []:
                    _walk_comment_tree(child, out)
            elif kind == "t1":
                d = node.get("data") or {}
                cid = (d.get("id") or "").lower()
                if cid:
                    out[cid] = d
                replies = d.get("replies")
                if isinstance(replies, dict):
                    _walk_comment_tree(replies, out)
        elif isinstance(node, list):
            for el in node:
                _walk_comment_tree(el, out)

    import xml.etree.ElementTree as _ET_grp
    _proxy_grp = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
    _rss_base_grp = _proxy_grp.rstrip("/") if _proxy_grp else "https://www.reddit.com"
    for post_url, group in post_groups.items():
        if len(group) < 2:
            continue  # singleton — per-item path handles it
        # Bulk pre-pass via the POST-LEVEL comment RSS. Reddit's JSON
        # API is auth-gated for cloud IPs (403), so the old
        # /comments/<id>.json fetch always failed and wasted ~12s of
        # retries per post before falling to the per-item loop. The
        # post's .rss feed returns up to ~50 comments in one call and
        # IS served through the proxy. We use it to mark LIVE in bulk;
        # any comment NOT found in the feed is left UNHANDLED so the
        # per-item permalink-RSS check (definitive — distinguishes
        # 'beyond the 50-cap' from 'actually removed') decides it.
        present_ids = set()
        feed_ok = False
        try:
            pm = _re_grp.search(r"(/r/[^/]+/comments/[a-z0-9]+)", post_url, _re_grp.IGNORECASE)
            if pm:
                rss_url = f"{_rss_base_grp}{pm.group(1)}/.rss"
                resp = _requests.get(
                    rss_url, params={"limit": 100},
                    headers={"User-Agent": REDDIT_USER_AGENT,
                             "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.5"},
                    timeout=15, proxies={"http": None, "https": None},
                )
                if resp.status_code == 200 and (resp.text or "").lstrip().startswith("<?xml"):
                    feed_ok = True
                    root = _ET_grp.fromstring(resp.text)
                    ns = {"atom": "http://www.w3.org/2005/Atom"}
                    for e in root.findall("atom:entry", ns):
                        m2 = _re_grp.search(r"t1_(\w+)", (e.find("atom:id", ns).text or ""))
                        if m2:
                            present_ids.add(m2.group(1).lower())
        except Exception as e:
            print(f"[{log_prefix}] bulk RSS fetch failed for {post_url}: {e}", flush=True)
            feed_ok = False

        if not feed_ok:
            # Couldn't read the post feed — leave the whole group to the
            # per-item loop (which RSS-checks each comment individually).
            continue

        # Mark each comment present in the feed as LIVE. Absent ones
        # are left unhandled (per-item permalink RSS will decide
        # live/removed definitively).
        for item, comment_id in group:
            if comment_id in present_ids:
                checked += 1
                _mark_live(item)
                live += 1
                handled_ids.add(item["id"])
                _emit_progress()
            # else: not in top-100 feed — could be removed OR just
            # nested/low-ranked. Leave for the per-item permalink check.

        _time.sleep(0.5)

    # =====================================================================
    # Per-item loop: handles search_comments, /s/ short URLs, singleton
    # Live Subs items, and any items whose bulk fetch failed. Sleep
    # between iterations stays at 3s for safety because individual
    # comment URLs are more likely to trip Reddit's anti-bot defenses.
    # =====================================================================
    for item in deployed:
        if item["id"] in handled_ids:
            continue
        checked += 1
        raw_url = item["reddit_comment_url"]
        src = item.get("source", "comment")
        if not raw_url:
            # A comment without a Reddit URL can't have been posted —
            # treat it as not-live so the dashboard's derived_status
            # rule flips its parent HQ Mention to 'removed'. Without
            # this the row would silently stay in 'report' (= live)
            # forever even though there's no actual mention on Reddit.
            print(f"[{log_prefix}] #{item['id']} ({src}): no URL — marking removed", flush=True)
            _mark_dead(item)
            dead += 1
            _emit_progress()
            continue

        # Clean URL: strip query params, trailing slash
        clean_url = raw_url.strip().split("?")[0].rstrip("/")

        # Resolve Reddit share/short URLs (/s/ links) via Cloudflare proxy
        if "/s/" in clean_url:
            import re as _re2
            try:
                # Extract path from URL, route through proxy's /resolve/ endpoint
                s_path = _re2.sub(r'^https?://[^/]+', '', clean_url)  # e.g. /r/sub/s/xxxx
                proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
                if proxy:
                    resolve_url = f"{proxy.rstrip('/')}/resolve{s_path}"
                    print(f"[{log_prefix}] #{item['id']} ({src}) resolving short URL via proxy...", flush=True)
                    r = _requests.get(resolve_url, timeout=15)
                    resolved = r.json().get("url", "").split("?")[0].rstrip("/")
                else:
                    # No proxy — try direct (works from residential IPs)
                    print(f"[{log_prefix}] #{item['id']} ({src}) resolving short URL direct...", flush=True)
                    r = _requests.head(clean_url, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    }, allow_redirects=True, timeout=15)
                    resolved = r.url.split("?")[0].rstrip("/")

                if "/comments/" in resolved:
                    clean_url = resolved
                    print(f"[{log_prefix}] #{item['id']} ({src}) resolved to {clean_url}", flush=True)
                else:
                    print(f"[{log_prefix}] #{item['id']} ({src}) short URL resolved to non-comment: {resolved}", flush=True)
                    changes.append({"id": item["id"], "source": src, "url": raw_url,
                                    "action": "skipped", "prev_status": item.get("status", ""), "new_status": ""})
                    errors += 1
                    error_details["bad_url"] += 1
                    _time.sleep(3)
                    continue
            except Exception as e:
                print(f"[{log_prefix}] #{item['id']} ({src}) failed to resolve short URL: {e}", flush=True)
                changes.append({"id": item["id"], "source": src, "url": raw_url,
                                "action": "skipped", "prev_status": item.get("status", ""), "new_status": ""})
                errors += 1
                error_details["bad_url"] += 1
                _time.sleep(3)
                continue

        if "/comment/" not in clean_url and "/comments/" not in clean_url:
            print(f"[{log_prefix}] Skipping #{item['id']} ({src}): not a comment URL: {clean_url}", flush=True)
            errors += 1
            error_details["bad_url"] += 1
            continue

        # PRIMARY liveness check: comment-permalink RSS. Reddit's JSON
        # API is auth-gated for cloud IPs (always 403), but the RSS
        # feed is served — and a comment's permalink RSS includes the
        # comment when LIVE and omits it when REMOVED. This is the only
        # working anonymous removal signal. If it's conclusive we act
        # on it and skip the (doomed) JSON fetch entirely. If it's
        # inconclusive (None), fall through to the legacy JSON path.
        rss_verdict = _comment_liveness_via_rss(clean_url)
        if rss_verdict == "live":
            print(f"[{log_prefix}] #{item['id']} ({src}) LIVE (comment RSS)", flush=True)
            _mark_live(item)
            live += 1
            handled_ids.add(item["id"])
            _emit_progress()
            _time.sleep(1)
            continue
        elif rss_verdict == "removed":
            print(f"[{log_prefix}] #{item['id']} ({src}) REMOVED (comment RSS — id absent from feed)", flush=True)
            _mark_dead(item)
            dead += 1
            handled_ids.add(item["id"])
            _emit_progress()
            _time.sleep(1)
            continue
        # else: rss_verdict is None (inconclusive) → fall through to JSON.

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
            removed_flag = comment.get("removed", False)
            collapsed_reason = comment.get("collapsed_reason_code", "")
            # Reddit-side publish timestamp from the JSON we just
            # fetched. Pass to _mark_dead so the 14-day chooser has
            # something to compare even when our DB column is NULL —
            # no second Reddit round trip needed.
            from bulk_deploy import _utc_seconds_to_iso as _utc_iso_per
            posted_hint_per_item = _utc_iso_per(comment.get("created_utc")) or None

            body_stripped = body.strip().lower()
            is_removed = (
                body_stripped in ("[deleted]", "[removed]") or
                "removed by reddit" in body_stripped or
                "removed by moderator" in body_stripped or
                removed_flag is True or
                author in ("[deleted]", "[removed]")
            )
            if is_removed:
                print(f"[{log_prefix}] #{item['id']} ({src}) DEAD body={body[:60]!r} author={author} removed={removed_flag}", flush=True)
                _mark_dead(item, posted_at_hint=posted_hint_per_item)
                dead += 1
            elif not body.strip() and not author:
                print(f"[{log_prefix}] #{item['id']} ({src}) DEAD empty body+author", flush=True)
                _mark_dead(item, posted_at_hint=posted_hint_per_item)
                dead += 1
            else:
                print(f"[{log_prefix}] #{item['id']} ({src}) LIVE author={author} body={body[:40]!r}", flush=True)
                _mark_live(item)
                live += 1
                # Piggy-back engagement stats: we already have the
                # parsed JSON. Cheap to persist so the portal
                # dashboard / CSV can render them without a separate
                # Reddit fetch. NULL-safe — Reddit always returns
                # `score` on live comments; `replies` may be a string
                # placeholder when there are zero replies.
                try:
                    replies_obj = comment.get("replies", "")
                    num_replies = 0
                    if isinstance(replies_obj, dict):
                        num_replies = len(replies_obj.get("data", {}).get("children", []))
                    db.update_live_stats_by_id_url(
                        item["id"], item.get("reddit_comment_url", ""),
                        comment.get("score"), num_replies,
                    )
                except Exception as stats_err:
                    print(f"[{log_prefix}] #{item['id']} stats persist failed: {stats_err}", flush=True)
                # Piggy-back the post-side checks on the comment JSON
                # we already fetched (data[0] is the post listing).
                # For Live Subs comments only — search_comments have
                # no Live Subs post container.
                if src == "comment":
                    try:
                        post_children = (data[0] or {}).get("data", {}).get("children", [])
                        post_data = (post_children[0] or {}).get("data", {}) if post_children else {}
                        # Resolve parent post id once.
                        pid_row = db.conn.execute(
                            "SELECT post_id FROM comments WHERE id = ?",
                            (item["id"],)
                        ).fetchone()
                        parent_post_id = pid_row["post_id"] if pid_row else None

                        # 1. posted_at backfill (free Reddit-side
                        #    publish timestamp for the post — the
                        #    client portal renders this as Published
                        #    Date). NULL-safe + only writes when
                        #    posts.posted_at is still NULL.
                        if post_data:
                            from bulk_deploy import _utc_seconds_to_iso
                            cu = post_data.get("created_utc")
                            iso = _utc_seconds_to_iso(cu) if cu else None
                            if iso and parent_post_id:
                                db.set_post_posted_at(parent_post_id, iso)

                        # 2. Post-liveness check. The client portal's
                        #    "Update status & stats" used to verify
                        #    comments but never the parent post — so
                        #    a post mods removed on Reddit kept
                        #    showing 'Live' on the dashboard. Now we
                        #    inspect the post listing: empty children,
                        #    [removed]/[deleted] selftext, or
                        #    author='[deleted]' all flip the post to
                        #    status='removed' (preserving prev_status
                        #    via mark_post_removed). The HQ Mentions
                        #    derived_status will then read 'removed'
                        #    on next portal render.
                        if parent_post_id:
                            post_is_dead = False
                            if not post_children:
                                post_is_dead = True
                            elif post_data:
                                stxt = (post_data.get("selftext") or "").strip().lower()
                                author = (post_data.get("author") or "").strip().lower()
                                if (post_data.get("removed") is True
                                        or stxt in ("[removed]", "[deleted]")
                                        or author in ("[deleted]", "[removed]")):
                                    post_is_dead = True
                            cur_post = db.conn.execute(
                                "SELECT status FROM posts WHERE id = ?",
                                (parent_post_id,)
                            ).fetchone()
                            cur_status = cur_post["status"] if cur_post else None
                            if post_is_dead and cur_status not in ("removed", None):
                                # Reuse the existing helper — flips
                                # status='removed' + saves
                                # prev_status. Idempotent.
                                db.conn.execute(
                                    "UPDATE posts SET status='removed', prev_status=? WHERE id=? AND status != 'removed'",
                                    (cur_status, parent_post_id),
                                )
                                db.conn.commit()
                                print(f"[{log_prefix}] post #{parent_post_id} marked removed (Reddit shows it gone)", flush=True)
                    except Exception as pp_err:
                        print(f"[{log_prefix}] #{item['id']} post-side piggy-back failed: {pp_err}", flush=True)

        except (_requests.exceptions.Timeout, _requests.exceptions.ConnectionError) as e:
            print(f"[{log_prefix}] #{item['id']} ({src}) {type(e).__name__}: {e}", flush=True)
            errors += 1
            error_details["timeout"] += 1
        except Exception as e:
            print(f"[{log_prefix}] #{item['id']} ({src}) error: {e}", flush=True)
            errors += 1
            error_details["exception"] += 1
        _emit_progress()
        # End-of-iteration pacing. The proxy already handles per-IP
        # rate limiting and _reddit_get retries 429/5xx automatically,
        # so we don't need the full 3s gap between successful fetches.
        # Reserve heavier sleeps for the error branches above (which
        # already use 3s/5s).
        _time.sleep(1)

    # Final progress write so the polling UI sees a clean 'N of N'
    # state right before the task completes.
    _emit_progress()
    return {"checked": checked, "live": live, "dead": dead, "errors": errors,
            "restored": restored, "changes": changes, "error_details": error_details}


@app.route("/api/all-comments/check-live", methods=["POST"])
def api_check_live_all_comments():
    """Check comments against Reddit with optional filters."""
    data = request.get_json() or {}
    f_status = data.get("status") or None
    f_brand_id = int(data["brand_id"]) if data.get("brand_id") else None
    f_subreddit_id = int(data["subreddit_id"]) if data.get("subreddit_id") else None
    f_account_id = data.get("account_id") or None
    f_source = data.get("source") or None
    f_date = data.get("date") or None
    f_fresh = data.get("fresh", False)

    def task():
        db = Database(DB_PATH)
        db.connect()
        db.initialize()
        try:
            comments = db.get_filtered_comment_urls(
                status=f_status, brand_id=f_brand_id,
                subreddit_id=f_subreddit_id, account_id=f_account_id,
                source_filter=f_source, date=f_date,
                fresh_only=f_fresh,
            )
            label = "fresh" if f_fresh else (f_status or "all")
            return _check_live_batch(comments, db, f"CHECK-LIVE-ALL({label})")
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


@app.route("/api/bulk-deploy/from-sheet", methods=["POST"])
def api_bulk_deploy_from_sheet():
    """Bulk-deploy a sheet's worth of Reddit URLs.

    Body: {"sheet_url": "https://docs.google.com/spreadsheets/d/..../edit"}
    The sheet must be set to "Anyone with the link → Viewer".

    Returns {"task_id": "..."} immediately. The client polls
    `GET /api/tasks/<task_id>` for progress + final report. See
    bulk_deploy.run_bulk_deploy for the per-URL matching strategy.
    """
    from bulk_deploy import run_bulk_deploy

    data = request.get_json() or {}
    sheet_url = (data.get("sheet_url") or "").strip()
    if not sheet_url:
        return jsonify({"error": "sheet_url is required"}), 400
    # `source` scopes the matcher to a single pipeline. The Bulk Deploy
    # modal sets this based on the calling view so URLs only match the
    # tables the user is operating on (avoids accidentally hitting the
    # OTHER pipeline's spurious copies).
    source_filter = data.get("source")
    if source_filter not in (None, "", "comment", "search_comment"):
        return jsonify({
            "error": "source must be 'comment' or 'search_comment' or omitted"
        }), 400
    source_filter = source_filter or None

    # Open a fresh DB on the background thread (request-thread DB
    # connections are not shareable across threads).
    def _db_factory():
        d = Database(DB_PATH)
        d.connect()
        d.initialize()
        return d

    def _task(_task_id=None):
        return run_bulk_deploy(
            sheet_url,
            db_factory=_db_factory,
            reddit_get=_reddit_get_json,
            resolve_share=_resolve_reddit_share_url_proxy,
            source_filter=source_filter,
            comment_meta_fetcher=_reddit_comment_meta_via_rss,
            _task_id=_task_id,
        )

    tid = start_task("bulk-deploy", _task, pass_task_id=True)
    return jsonify({"task_id": tid})


# =============================================================================
# Check Live — admin-side standalone health checker.
#
# Takes a Google Sheet with a "Comment Link" column (same shape as
# Bulk Deploy), classifies every URL as live/removed/missing/error
# using the existing fetch_reddit_comment_meta + classify_liveness
# helpers, and persists the run + per-URL details against a name
# the admin assigns. Does NOT touch any comment rows in the main DB
# — purely a reporting tool.
# =============================================================================

@app.route("/api/check-live/runs", methods=["GET"])
def api_check_live_list_runs():
    db = get_db()
    try:
        return jsonify({"runs": db.list_check_live_runs(limit=100)})
    finally:
        db.close()


@app.route("/api/check-live/removed-comments", methods=["GET"])
def api_check_live_removed_comments():
    """All comments currently marked removed / replace (with brand, subreddit, account)
    for the Check Live → Analyse view. The UI does all filtering + per-brand/subreddit/
    account aggregation client-side from this one list."""
    db = get_db()
    try:
        return jsonify({"items": db.get_removed_comments(),
                        "live": db.get_live_comment_counts()})
    finally:
        db.close()


@app.route("/api/check-live/runs/<int:run_id>", methods=["GET"])
def api_check_live_get_run(run_id):
    db = get_db()
    try:
        run = db.get_check_live_run(run_id)
        if not run:
            return jsonify({"error": "not_found"}), 404
        return jsonify(run)
    finally:
        db.close()


@app.route("/api/check-live/runs/<int:run_id>", methods=["DELETE"])
def api_check_live_delete_run(run_id):
    db = get_db()
    try:
        db.delete_check_live_run(run_id)
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/check-live/start", methods=["POST"])
def api_check_live_start():
    """Body: {name: str, sheet_url: str}.
    Creates a check_live_runs row, then spawns a background task that:
      1. Fetches the sheet CSV.
      2. Extracts Reddit URLs from the "Comment Link" column.
      3. For each URL, calls fetch_reddit_comment_meta + classify.
      4. Stores per-URL detail rows on check_live_run_results.
      5. Flips the run header to status='complete' when done.
    Returns {run_id, task_id}.
    """
    import time as _time
    from bulk_deploy import (
        fetch_sheet_csv, extract_reddit_rows, classify_reddit_url,
        fetch_reddit_comment_meta, classify_liveness,
    )

    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    sheet_url = (data.get("sheet_url") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not sheet_url:
        return jsonify({"error": "sheet_url is required"}), 400

    db = get_db()
    try:
        run_id = db.create_check_live_run(name, sheet_url)
    finally:
        db.close()

    def task():
        bg = Database(DB_PATH)
        bg.connect()
        bg.initialize()
        try:
            try:
                csv_text = fetch_sheet_csv(sheet_url)
            except Exception as e:
                bg.finish_check_live_run(run_id, status='error',
                                          error_detail=f"sheet fetch failed: {e}")
                return {"run_id": run_id, "error": str(e)}
            rows = extract_reddit_rows(csv_text)
            if not rows:
                bg.finish_check_live_run(run_id, status='error',
                                          error_detail="no Reddit URLs found in sheet")
                return {"run_id": run_id, "error": "no_urls"}

            def _classify_post_url(post_path):
                """Lightweight liveness check for a POST URL. Reddit
                returns a list with the post listing first; an empty
                children list means the post was removed/deleted.
                """
                try:
                    data = _reddit_get_json(post_path)
                except Exception as e:
                    return ('error', str(e))
                if not data:
                    return ('missing', 'fetch returned no data')
                if not isinstance(data, list) or len(data) < 1:
                    return ('missing', 'unexpected JSON shape')
                children = (data[0] or {}).get('data', {}).get('children', [])
                if not children:
                    return ('removed', 'post listing empty')
                cd = (children[0] or {}).get('data', {}) or {}
                body = (cd.get('selftext') or '').strip().lower()
                if cd.get('removed') is True or body in ('[removed]', '[deleted]'):
                    return ('removed', '[removed]/[deleted] selftext or flag')
                if cd.get('author') in ('[deleted]', '[removed]'):
                    return ('removed', f"author={cd.get('author')}")
                return ('live', None)

            for row in rows:
                url = row["url"]
                # If extract_reddit_rows flagged this row as
                # un-checkable (empty Comment Link cell, non-Reddit
                # URL, etc.) emit a 'missing' result row right away
                # so the total matches the sheet's row count and the
                # operator can see exactly which rows were skipped
                # and why.
                if row.get("skip_reason") or not url:
                    bg.add_check_live_result(
                        run_id, url=(url or ""),
                        comment_id=row.get("comment_id"),
                        post_id=None,
                        liveness="missing",
                        detail=row.get("skip_reason") or "no URL in Comment Link cell",
                    )
                    continue
                # 1. Resolve /s/ short URLs to their canonical form.
                #    Bulk Deploy does this; we were silently skipping
                #    it, causing every /s/ row to come back 'missing'.
                resolved = url
                try:
                    if '/s/' in url:
                        r = _resolve_reddit_share_url_proxy(url)
                        if r and '/comments/' in r:
                            resolved = r
                except Exception:
                    resolved = url

                # 2. Classify the resolved URL.
                classified = classify_reddit_url(resolved)
                # The operator's sheet may carry an explicit comment
                # ID column (assigned in their workflow). Prefer that
                # over the one we'd parse from the URL — the user is
                # tracking these by their sheet's IDs, not Reddit's.
                # Fall back to the URL-parsed id when the sheet has
                # no ID column or the cell is blank.
                comment_id = row.get("comment_id") or (
                    (classified or {}).get("comment_id") if classified else None
                )
                post_id = (classified or {}).get("post_id") if classified else None

                def _probe():
                    """One classification pass for the resolved URL.
                    Returned tuple: (liveness, detail). Pulled out so
                    we can re-run on transient 'missing' verdicts.

                    PRIMARY path is RSS-based: Reddit's JSON API is
                    auth-gated for cloud IPs (always 403), so the old
                    fetch_reddit_comment_meta/_reddit_get_json path
                    returned nothing → everything came back 'missing'.
                    The comment-permalink RSS feed includes the comment
                    when live and omits it when removed — a reliable
                    signal that works through the proxy. JSON stays as a
                    fallback for environments where it's reachable.
                    """
                    if not classified:
                        return 'missing', 'URL did not match Reddit comment/post pattern'
                    if classified.get('kind') == 'comment':
                        # RSS first — the working anonymous signal.
                        rss_v = _comment_liveness_via_rss(resolved)
                        if rss_v == 'live':
                            return 'live', 'via comment-permalink RSS'
                        if rss_v == 'removed':
                            return 'removed', 'comment id absent from its permalink RSS feed'
                        # rss_v is None (inconclusive) → try JSON as fallback.
                        if comment_id:
                            try:
                                meta = fetch_reddit_comment_meta(
                                    resolved, reddit_get=_reddit_get_json,
                                )
                                lv = classify_liveness(meta)
                                if lv == 'missing':
                                    return lv, "RSS inconclusive + comment id not in JSON response (transient — proxy/rate-limit, or comment not in thread tree)"
                                return lv, None
                            except Exception as e:
                                return 'error', str(e)
                        return 'missing', 'RSS inconclusive and no comment id to JSON-check'
                    # POST url: try the post's RSS feed first, then JSON.
                    post_rss = _post_liveness_via_rss(resolved)
                    if post_rss in ('live', 'removed'):
                        return post_rss, f'post via RSS ({post_rss})'
                    try:
                        from urllib.parse import urlparse
                        path = urlparse(resolved).path.rstrip('/') + '.json'
                        return _classify_post_url(path)
                    except Exception as e:
                        return 'error', str(e)

                liveness, detail = _probe()
                # Most 'missing' verdicts are transient — Reddit
                # randomly fails to return a specific comment in the
                # thread tree, the proxy hiccups, or we hit a soft
                # rate-limit. Retry once after a longer pause before
                # marking the row missing for real.
                if liveness == 'missing':
                    _time.sleep(4)
                    retry_lv, retry_detail = _probe()
                    if retry_lv != 'missing':
                        liveness, detail = retry_lv, retry_detail
                    else:
                        # Append the retry note so the operator can
                        # tell apart 'truly missing both attempts'
                        # from a first-pass blip.
                        detail = (detail or '') + ' (retried once, still missing)'

                # Annotate detail with the resolved URL when we
                # rewrote it — helps the user spot /s/ inputs.
                if resolved != url and detail:
                    detail = f"(resolved to {resolved}) {detail}"
                elif resolved != url:
                    detail = f"resolved from /s/ → {resolved}"

                bg.add_check_live_result(
                    run_id, url=url, comment_id=comment_id,
                    post_id=post_id, liveness=liveness, detail=detail,
                )
                # Be polite — same pacing as the bulk deploy matcher.
                _time.sleep(1)
            bg.finish_check_live_run(run_id, status='complete')
            return {"run_id": run_id, "ok": True}
        finally:
            try: bg.close()
            except Exception: pass

    tid = start_task("check-live-sheet", task)
    return jsonify({"run_id": run_id, "task_id": tid})


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
                item.setdefault("status", "deployed")
            return _check_live_batch(deployed, db, "CHECK-LIVE-SEARCH")
        finally:
            db.close()

    tid = start_task("check-live-search", task)
    return jsonify({"task_id": tid})


@app.route("/api/check-live/logs", methods=["GET"])
def api_check_live_logs():
    db = get_db()
    try:
        limit = request.args.get("limit", 100, type=int)
        offset = request.args.get("offset", 0, type=int)
        action = request.args.get("action") or None
        source = request.args.get("source") or None
        logs = db.get_live_check_logs(limit=limit, offset=offset, action=action, source=source)
        return jsonify(logs)
    finally:
        db.close()


@app.route("/api/check-live/revert/preview", methods=["GET"])
def api_check_live_revert_preview():
    """Read-only: identify recent check-live 'runs' and (optionally)
    preview exactly what reverting a window would change. Nothing is
    modified. Use this to find the run that flipped your statuses.

    Query params:
      hours   — look-back window for the run list (default 48)
      minutes — convenience: preview reverting everything changed in the
                last N minutes
      since / until — explicit UTC 'YYYY-MM-DD HH:MM:SS' bounds to preview
      action  — repeatable; limit to 'marked_dead' and/or 'restored'
      brand   — optional brand name; scope runs + preview to one brand
    """
    db = get_db()
    try:
        hours = request.args.get("hours", 48, type=int)
        brand = request.args.get("brand") or None
        runs = db.get_check_live_log_runs(hours=hours, brand_name=brand)
        since = request.args.get("since") or None
        until = request.args.get("until") or None
        minutes = request.args.get("minutes", type=int)
        if minutes and not since:
            since = db.conn.execute(
                "SELECT datetime('now', ?)", (f"-{int(minutes)} minutes",)
            ).fetchone()[0]
        actions = request.args.getlist("action") or None
        preview = None
        if since or until:
            preview = db.revert_check_live_window(
                since=since, until=until, actions=actions,
                brand_name=brand, dry_run=True)
        return jsonify({"runs": runs, "preview": preview})
    finally:
        db.close()


@app.route("/api/check-live/revert", methods=["POST"])
def api_check_live_revert():
    """Revert a window of check-live status changes back to each row's
    prior status (from the check_live_log audit trail).

    Body: {minutes? | since?/until?, actions?, brand?, confirm?}
      - Provide `minutes` (last N minutes) OR `since`/`until`
        (UTC 'YYYY-MM-DD HH:MM:SS').
      - `actions` (optional): subset of ['marked_dead','restored'].
      - `brand` (optional): scope the revert to one brand's rows.
      - confirm must be true to APPLY; otherwise a dry-run preview is
        returned (changes nothing).
    """
    data = request.get_json() or {}
    since = data.get("since")
    until = data.get("until")
    minutes = data.get("minutes")
    actions = data.get("actions")
    brand = data.get("brand") or None
    confirm = bool(data.get("confirm"))
    db = get_db()
    try:
        if minutes and not since:
            since = db.conn.execute(
                "SELECT datetime('now', ?)", (f"-{int(minutes)} minutes",)
            ).fetchone()[0]
        if not since and not until:
            return jsonify({
                "error": "provide `minutes`, or `since`/`until` "
                         "(UTC 'YYYY-MM-DD HH:MM:SS')"
            }), 400
        report = db.revert_check_live_window(
            since=since, until=until, actions=actions,
            brand_name=brand, dry_run=not confirm)
        return jsonify(report)
    finally:
        db.close()


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
    """Fetch live Reddit stats (upvotes, replies) for a list of comment URLs.

    Mirrors the robustness of _check_live_batch:
    - Resolves /s/ share-URLs through the proxy (or HEAD-redirect fallback).
    - Strips every common Reddit subdomain (www / old / new / np / m).
    - Treats 404 as removed, retries 429 / 5xx once with backoff, surfaces 403,
      handles HTML-instead-of-JSON responses gracefully.
    - Per-cid liveness lets the CSV exporter distinguish "removed" / "rate-
      limited" / "fetch-failed" so missing rows are explainable.
    """
    import time as _time
    import re as _re
    import requests as _requests

    data = request.json or {}
    urls = data.get("urls", [])  # list of {id, reddit_comment_url}

    def _resolve_share(short_url):
        """Best-effort resolution of an /s/ share link to its /comments/ URL."""
        proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
        s_path = _re.sub(r'^https?://[^/]+', '', short_url)
        try:
            if proxy:
                r = _requests.get(f"{proxy.rstrip('/')}/resolve{s_path}", timeout=15)
                resolved = (r.json().get("url") or "").split("?")[0].rstrip("/")
            else:
                r = _requests.head(short_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }, allow_redirects=True, timeout=15)
                resolved = r.url.split("?")[0].rstrip("/")
            return resolved if "/comments/" in resolved else ""
        except Exception:
            return ""

    def _parse_comment_body(rdata):
        """Pull (score, author, num_replies, permalink, created_utc, liveness)
        out of a Reddit comment JSON response. Returns None if the response
        doesn't contain a t1 (comment) child."""
        if not isinstance(rdata, list) or len(rdata) < 2:
            return None
        children = rdata[1].get("data", {}).get("children", [])
        for child in children:
            if child.get("kind") != "t1":
                continue
            cd = child.get("data", {})
            replies_obj = cd.get("replies", "")
            num_replies = 0
            if isinstance(replies_obj, dict):
                num_replies = len(replies_obj.get("data", {}).get("children", []))
            body_text = cd.get("body", "")
            is_removed = body_text in ("[deleted]", "[removed]")
            return {
                "score": cd.get("score", 0),
                "author": cd.get("author", ""),
                "num_replies": num_replies,
                "permalink": cd.get("permalink", ""),
                "created_utc": cd.get("created_utc", 0),
                "liveness": "removed" if is_removed else "live",
            }
        return None

    def _fetch_one(json_path, attempt=1):
        """Single fetch with one retry on 429/5xx/timeout."""
        try:
            resp = _reddit_get(json_path, timeout=15)
        except Exception as e:
            if attempt < 2:
                _time.sleep(3)
                return _fetch_one(json_path, attempt + 1)
            return {"error": "fetch_exception", "detail": str(e)}

        sc = resp.status_code
        if sc == 200:
            try:
                return {"data": resp.json()}
            except Exception:
                return {"error": "non_json"}
        if sc == 404:
            return {"removed": True}
        if sc == 403:
            return {"error": "forbidden"}
        if sc == 429 or sc >= 500:
            if attempt < 2:
                _time.sleep(5)
                return _fetch_one(json_path, attempt + 1)
            return {"error": "rate_limited" if sc == 429 else f"http_{sc}"}
        return {"error": f"http_{sc}"}

    def task():
        results = {}
        total = len(urls)
        # Piggy-back: every time we successfully resolve a comment's
        # created_utc, we also patch posted_at on the DB row. Cheap
        # — same JSON we already parsed, one extra UPDATE per row.
        # Opened once per task; closed in `finally`.
        bg_db = Database(DB_PATH)
        try:
            bg_db.connect()
            bg_db.initialize()
        except Exception as e:
            print(f"[LIVE-STATS] bg db open failed: {e}", flush=True)
            bg_db = None
        try:
            for i, item in enumerate(urls):
                cid = item.get("id")
                url = (item.get("reddit_comment_url") or "").strip()
                key = str(cid)
                if not url:
                    results[key] = {"liveness": "no_url"}
                    continue

                clean = url.split("?")[0].rstrip("/")
                if "/s/" in clean:
                    resolved = _resolve_share(clean)
                    if not resolved:
                        results[key] = {"liveness": "share_unresolved"}
                        _time.sleep(1)
                        continue
                    clean = resolved

                if "/comment/" not in clean and "/comments/" not in clean:
                    results[key] = {"liveness": "bad_url"}
                    continue

                # Strip every common Reddit subdomain (was hardcoded www only)
                path = _re.sub(r'^https?://(?:www\.|old\.|new\.|np\.|m\.)?reddit\.com', '', clean)
                if not path.startswith('/'):
                    results[key] = {"liveness": "bad_url"}
                    continue
                json_path = path + ".json"

                res = _fetch_one(json_path)
                if "removed" in res:
                    results[key] = {
                        "score": 0, "author": "", "num_replies": 0,
                        "permalink": "", "created_utc": 0,
                        "liveness": "removed",
                    }
                elif "data" in res:
                    parsed = _parse_comment_body(res["data"])
                    if parsed:
                        results[key] = parsed
                        # Piggy-back the posted_at backfill — only when
                        # the parsed `created_utc` is a real value and
                        # the row in the DB still has NULL posted_at.
                        try:
                            from bulk_deploy import _utc_seconds_to_iso
                            posted_at = _utc_seconds_to_iso(parsed.get("created_utc"))
                            if posted_at and bg_db and cid:
                                bg_db.update_posted_at_by_id_url(cid, url, posted_at)
                        except Exception as bg_err:
                            print(f"[LIVE-STATS] posted_at piggy-back error cid={cid}: {bg_err}", flush=True)
                        # Persist the engagement numbers so the client
                        # portal dashboard / CSV can render them without
                        # re-hitting Reddit. id might be a source-prefixed
                        # string for some callers — guard with int().
                        try:
                            int_cid = int(cid) if isinstance(cid, (int, str)) and str(cid).isdigit() else None
                            if int_cid and bg_db:
                                bg_db.update_live_stats_by_id_url(
                                    int_cid, url,
                                    parsed.get("score"),
                                    parsed.get("num_replies"),
                                )
                        except Exception as bg_err:
                            print(f"[LIVE-STATS] persist error cid={cid}: {bg_err}", flush=True)
                    else:
                        results[key] = {"liveness": "no_comment_in_response"}
                else:
                    # error case — record it so the caller knows why this row is empty
                    results[key] = {"liveness": res.get("error") or "fetch_failed"}

                # Modest pacing — _reddit_get is already proxied; 1s/req is safe.
                # Bump to 3s after a transient error to be polite.
                _time.sleep(3 if "error" in res else 1)
            print(f"[LIVE-STATS] processed {total} url(s); successes={sum(1 for v in results.values() if v.get('liveness') == 'live')}", flush=True)
            return results
        finally:
            if bg_db:
                try:
                    bg_db.close()
                except Exception:
                    pass

    tid = start_task("live-stats", task)
    return jsonify({"task_id": tid})


@app.route("/api/comments/reconcile-replace-window", methods=["POST"])
def api_reconcile_replace_window():
    """One-shot: walk every 'removed' row whose anchor timestamp
    (posted_at, falling back to deployed_at) falls inside the
    14-day replace window and flip it to 'replace'.

    Useful when 'replace' was introduced after some comments were
    already auto-marked 'removed' — those rows otherwise stay in
    'removed' forever since the chooser only runs at detection time.

    Body: optional {"days": <int>} to override the default window.
    Returns counts per table.
    """
    data = request.get_json(silent=True) or {}
    days = int(data["days"]) if data.get("days") else None
    db = get_db()
    try:
        promoted = db.reconcile_replace_window(days=days)
    finally:
        db.close()
    return jsonify({"ok": True, "promoted": promoted})


@app.route("/api/comments/replace-window-diagnose", methods=["GET"])
def api_replace_window_diagnose():
    """Diagnostic: list every 'removed' comment + the timestamps the
    chooser would inspect, plus days-since-publish and what the
    chooser WOULD pick if re-run right now. Use this when an admin
    thinks a removed row should be Replace but reconcile didn't
    promote it — the row's posted_at / deployed_at / created_at
    will reveal why.

    Query params:
      ?source=comment|search_comment  (default: comment — HQ pipeline)
      ?days=<int>                     (default: 14)
      ?limit=<int>                    (default: 50)
    """
    from datetime import datetime, timedelta
    src = (request.args.get("source") or "comment").strip().lower()
    if src not in ("comment", "search_comment"):
        return jsonify({"error": "source must be 'comment' or 'search_comment'"}), 400
    days = int(request.args.get("days") or 14)
    limit = int(request.args.get("limit") or 50)
    table = "comments" if src == "comment" else "search_comments"
    db = get_db()
    try:
        rows = db.conn.execute(
            f"""SELECT id, status, posted_at, deployed_at, created_at,
                       reddit_comment_url
                  FROM {table}
                 WHERE status = 'removed'
                 ORDER BY id DESC
                 LIMIT ?""",
            (limit,)
        ).fetchall()
        now = datetime.utcnow()
        out = []
        for r in rows:
            anchor = r["posted_at"] or r["deployed_at"]
            anchor_days = None
            chooser_pick = "removed"
            if anchor:
                try:
                    s = str(anchor).replace("T", " ").split(".")[0].strip()
                    ts = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
                    delta = now - ts
                    anchor_days = round(delta.total_seconds() / 86400.0, 2)
                    if delta.total_seconds() < 0 or delta.days < days:
                        chooser_pick = "replace"
                except Exception:
                    pass
            out.append({
                "id": r["id"],
                "url": r["reddit_comment_url"],
                "posted_at": r["posted_at"],
                "deployed_at": r["deployed_at"],
                "created_at": r["created_at"],
                "anchor_used": "posted_at" if r["posted_at"] else ("deployed_at" if r["deployed_at"] else None),
                "days_since_anchor": anchor_days,
                "chooser_would_pick": chooser_pick,
            })
        return jsonify({
            "ok": True,
            "source": src,
            "window_days": days,
            "count": len(out),
            "rows": out,
        })
    finally:
        db.close()


@app.route("/api/comments/refresh-replace-window", methods=["POST"])
def api_refresh_replace_window():
    """One-shot: backfill posted_at from Reddit for every 'removed'
    row that's missing it (and has a Reddit URL), then run reconcile.

    This is the "stronger" version of reconcile-replace-window — it
    closes the gap where a comment is genuinely within 14 days of
    publish on Reddit but our DB never persisted posted_at (so the
    chooser fell back to deployed_at or defaulted to 'removed').

    Body: optional {"days": <int>, "limit": <int>} — limit caps the
    Reddit fetch fan-out (default 200) to keep one run bounded.
    Returns counts per table.
    """
    from bulk_deploy import fetch_reddit_comment_meta
    data = request.get_json(silent=True) or {}
    days = int(data["days"]) if data.get("days") else None
    limit = int(data.get("limit", 200))
    db = get_db()
    backfilled = {"comments": 0, "search_comments": 0}
    try:
        for table, key in [("comments", "comments"),
                           ("search_comments", "search_comments")]:
            rows = db.conn.execute(
                f"""SELECT id, reddit_comment_url FROM {table}
                     WHERE status = 'removed'
                       AND posted_at IS NULL
                       AND TRIM(COALESCE(reddit_comment_url, '')) != ''
                     ORDER BY id DESC
                     LIMIT ?""",
                (limit,)
            ).fetchall()
            for r in rows:
                try:
                    meta = fetch_reddit_comment_meta(
                        r["reddit_comment_url"], reddit_get=_reddit_get_json
                    )
                    posted_at = meta.get("posted_at") if meta else None
                    if posted_at:
                        db.update_posted_at(r["id"], key, posted_at)
                        backfilled[key] += 1
                except Exception as e:
                    print(f"[refresh-replace] {key} #{r['id']}: backfill failed: {e}", flush=True)
                    continue
        promoted = db.reconcile_replace_window(days=days)
    finally:
        db.close()
    return jsonify({"ok": True, "backfilled": backfilled, "promoted": promoted})


@app.route("/api/comments/backfill-posted-at", methods=["POST"])
def api_backfill_posted_at():
    """Walk deployed comments missing `posted_at` and fetch Reddit's
    `created_utc` for each, writing it into the row.

    Body: {"brand_id"?: int, "subreddit_id"?: int}. Optional filters.
    Returns {"task_id": "..."} immediately; client polls
    GET /api/tasks/<task_id>.

    Idempotent — only touches rows that still have NULL `posted_at`.
    Re-running after a partial network blip picks up where it left off.
    """
    import time as _time
    data = request.get_json() or {}
    brand_id = data.get("brand_id")
    subreddit_id = data.get("subreddit_id")

    def task(_task_id=None):
        from bulk_deploy import fetch_reddit_comment_meta
        bg_db = Database(DB_PATH)
        bg_db.connect()
        bg_db.initialize()
        try:
            rows = bg_db.list_deployed_missing_posted_at(
                brand_id=brand_id, subreddit_id=subreddit_id, limit=2000,
            )
            total = len(rows)
            progress = {"total": total, "processed": 0,
                        "updated": 0, "failed": 0, "results": []}
            print(f"[BACKFILL-POSTED-AT] {total} rows to process "
                  f"(brand_id={brand_id}, subreddit_id={subreddit_id})", flush=True)
            for row in rows:
                cid = row["id"]
                kind = row["kind"]
                url = row["reddit_comment_url"]
                try:
                    meta = fetch_reddit_comment_meta(url, reddit_get=_reddit_get_json)
                    posted_at = meta.get("posted_at")
                except Exception as e:
                    posted_at = None
                    print(f"[BACKFILL-POSTED-AT] cid={cid} fetch error: {e}", flush=True)
                if posted_at:
                    try:
                        bg_db.update_posted_at(cid, kind, posted_at)
                        progress["updated"] += 1
                        progress["results"].append({
                            "id": cid, "kind": kind, "posted_at": posted_at,
                            "action": "updated",
                        })
                    except Exception as e:
                        progress["failed"] += 1
                        progress["results"].append({
                            "id": cid, "kind": kind,
                            "action": "error", "reason": str(e),
                        })
                else:
                    progress["failed"] += 1
                    progress["results"].append({
                        "id": cid, "kind": kind,
                        "action": "no_match",
                        "reason": "no created_utc in reddit response",
                    })
                progress["processed"] += 1
                if _task_id:
                    try:
                        bg_db.update_task_progress(_task_id, json.dumps(progress))
                    except Exception:
                        pass
                # Pacing — same shape as live-stats / check-live: 1s
                # per call, longer after a miss.
                _time.sleep(1 if posted_at else 2)
            return progress
        finally:
            try:
                bg_db.close()
            except Exception:
                pass

    tid = start_task("backfill-posted-at", task, pass_task_id=True)
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


# ---------------------------------------------------------------------------
# Live Subreddits — generate posts targeting a brand's saved sub list
# ---------------------------------------------------------------------------

# Module-level cache for subreddit-fit checks: {(brand_id, sub_name_lower): (ts, dict)}
_LIVE_SUB_FIT_CACHE = {}
_LIVE_SUB_FIT_TTL = 24 * 3600  # 24h

def _normalize_sub_name(name):
    """Strip r/ prefix and trailing slashes, lowercased."""
    s = (name or "").strip()
    if s.lower().startswith("r/"):
        s = s[2:]
    return s.strip("/").lower()


def _brand_search_subs(brand):
    """Parse the brand's search_subreddits JSON list. Returns lowercase names."""
    raw = brand.get("search_subreddits") if brand else None
    if not raw:
        return []
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(items, list):
            return []
        return [_normalize_sub_name(s) for s in items if str(s).strip()]
    except Exception:
        return []


def _check_subreddit_fit(brand, sub_name):
    """Ask Claude whether the brand's typical posts would fit r/<sub_name>.

    Pulls top 25 posts of the sub for 1-month window via the Reddit proxy,
    sends the title list + brand context to Claude, returns
    {fits: bool, reason: str, sample_titles: [...]}. Cached 24h per
    (brand_id, sub) so repeat clicks don't burn tokens.
    """
    bid = brand["id"]
    sname = _normalize_sub_name(sub_name)
    key = (bid, sname)
    import time as _time
    now = _time.time()
    cached = _LIVE_SUB_FIT_CACHE.get(key)
    if cached and (now - cached[0]) < _LIVE_SUB_FIT_TTL:
        return cached[1]

    titles = []
    try:
        resp = _reddit_get(f"/r/{sname}/top.json?limit=25&t=month", timeout=15)
        if resp.status_code == 200:
            jd = resp.json()
            for ch in jd.get("data", {}).get("children", []):
                t = ch.get("data", {}).get("title")
                if t:
                    titles.append(t)
    except Exception as e:
        print(f"[LIVE-FIT] fetch error r/{sname}: {e}", flush=True)

    if not titles:
        # Can't fetch — be permissive. Don't block; flag it.
        result = {"fits": True, "reason": "Could not fetch top posts; skipping fit check.", "sample_titles": []}
        _LIVE_SUB_FIT_CACHE[key] = (now, result)
        return result

    sample = "\n".join(f"  - {t}" for t in titles[:25])
    brand_ctx = (brand.get("context") or "").strip()[:1500]
    prompt = f"""You are checking whether posts about a brand's domain would fit on a specific subreddit.

BRAND CONTEXT (what the brand does / who it's for):
\"\"\"{brand_ctx or brand.get('name', '')}\"\"\"

TARGET SUBREDDIT: r/{sname}

TOP POSTS RECENTLY ON r/{sname} (sample of what the community actually posts):
{sample}

Decide whether posts about THIS BRAND'S domain would naturally fit r/{sname}.
- "fits" = true means a long-tail user query about the brand's category would be on-topic
  for this sub and likely get a useful response (not "wrong sub, try X" replies).
- "fits" = false means the sub is clearly off-topic for this brand (different field /
  audience), AND posting there would feel forced or get redirected.

Be lenient on close-but-different subs (a fitness brand on r/cooking is OFF; a fitness brand
on r/loseit is FINE). Only refuse on clearly mismatched pairs.

Return JSON only:
{{"fits": true|false, "reason": "one short sentence"}}"""
    try:
        from generators.base import ClaudeClient
        claude = ClaudeClient(ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", ""))
        out = claude.call(prompt, max_tokens=300, temperature=0.2)
        if isinstance(out, dict) and "fits" in out:
            result = {
                "fits": bool(out.get("fits")),
                "reason": str(out.get("reason") or ""),
                "sample_titles": titles[:10],
            }
        else:
            result = {"fits": True, "reason": "Fit check inconclusive; allowing.", "sample_titles": titles[:10]}
    except Exception as e:
        print(f"[LIVE-FIT] LLM error: {e}", flush=True)
        result = {"fits": True, "reason": f"Fit check failed: {e}", "sample_titles": titles[:10]}
    _LIVE_SUB_FIT_CACHE[key] = (now, result)
    return result


def _title_similarity(a, b):
    """Token-set ratio in [0,1] — a cheap stand-in for fuzzy matching."""
    import re as _re
    def _tokens(s):
        return set(_re.findall(r"[a-z0-9]+", (s or "").lower()))
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _live_reddit_dup(sub_name, candidate_title, sim_threshold=0.85, min_score=3):
    """Search r/<sub>/search.json for a candidate title; return the matching
    post dict (with title, score, url) if any existing post has similarity >=
    threshold AND score >= min_score, else None.
    """
    import urllib.parse as _u
    sname = _normalize_sub_name(sub_name)
    q = _u.quote(candidate_title[:200])
    try:
        resp = _reddit_get(f"/r/{sname}/search.json?q={q}&restrict_sr=on&limit=10&sort=relevance", timeout=15)
        if resp.status_code != 200:
            return None
        jd = resp.json()
        for ch in jd.get("data", {}).get("children", []):
            d = ch.get("data", {})
            existing = d.get("title", "")
            sim = _title_similarity(existing, candidate_title)
            if sim >= sim_threshold and (d.get("score") or 0) >= min_score:
                return {
                    "title": existing,
                    "score": d.get("score", 0),
                    "url": "https://www.reddit.com" + d.get("permalink", ""),
                    "similarity": round(sim, 3),
                }
    except Exception as e:
        print(f"[LIVE-DUP] search error r/{sname} '{candidate_title[:40]}': {e}", flush=True)
    return None


@app.route("/api/brands/<int:bid>/live-subreddits")
def api_brand_live_subreddits(bid):
    """Return the brand's saved Live Search subreddit list (the
    search_subreddits JSON column, already populated by the LS Save-to-brand
    button or the Edit Brand modal).

    Also returns `counts`: a {subreddit_name: {deployed, reported}} map
    so the Live Subs Generate-tab dropdown can label each option with
    its deployed + reported post counts at a glance. Counts are scoped
    to live posts for THIS brand (matches the brand-id filter the rest
    of the lsubs flow uses).
    """
    db = get_db()
    try:
        brand = db.get_brand(bid)
        if not brand:
            return jsonify({"error": "Brand not found"}), 404
        subs = _brand_search_subs(brand)
        # `deployed` here means posts.status='published' — that's the
        # actual on-Reddit live state for Live Subs posts. We
        # additionally surface 'paid' under deployed (it's a post-
        # publish admin state, still live on Reddit).
        # `reported` covers posts.status='report' so the dropdown
        # can hint how many posts for this brand × sub have been
        # pushed into a monthly client report.
        rows = db.conn.execute(
            """SELECT LOWER(s.name) AS sub_lc,
                      SUM(CASE WHEN p.status IN ('published','paid') THEN 1 ELSE 0 END) AS deployed,
                      SUM(CASE WHEN p.status = 'report' THEN 1 ELSE 0 END) AS reported
                 FROM posts p
                 JOIN subreddits s ON p.subreddit_id = s.id
                 LEFT JOIN post_brands pb ON pb.post_id = p.id
                WHERE COALESCE(s.is_live, 0) = 1
                  AND (p.brand_id = ? OR pb.brand_id = ?)
                GROUP BY LOWER(s.name)""",
            (bid, bid)
        ).fetchall()
        counts = {}
        for r in rows:
            counts[r["sub_lc"]] = {
                "deployed": r["deployed"] or 0,
                "reported": r["reported"] or 0,
            }
        return jsonify({"brand_id": bid, "subreddits": subs, "counts": counts})
    finally:
        db.close()


@app.route("/api/brands/<int:bid>/live-subreddits", methods=["POST"])
def api_brand_live_subreddits_add(bid):
    """Append a subreddit to the brand's saved live-subs list.
    Body: {subreddit_name: str}. Idempotent — re-adding a name that's
    already present is a no-op. The name is normalized (lowercased,
    r/ prefix stripped). Also auto-provisions a `subreddits` row via
    `ensure_live_subreddit` so the post pipeline can FK to it.
    """
    db = get_db()
    try:
        data = request.json or {}
        name = _normalize_sub_name(data.get("subreddit_name", ""))
        if not name:
            return jsonify({"error": "subreddit_name required"}), 400
        brand = db.get_brand(bid)
        if not brand:
            return jsonify({"error": "Brand not found"}), 404
        existing = _brand_search_subs(brand)
        if name in existing:
            return jsonify({"ok": True, "noop": True, "subreddits": existing})
        # Auto-provision the row so post-generator FKs resolve.
        db.ensure_live_subreddit(name)
        new_list = existing + [name]
        db.conn.execute(
            "UPDATE brands SET search_subreddits = ? WHERE id = ?",
            (json.dumps(new_list), bid)
        )
        db.conn.commit()
        return jsonify({"ok": True, "subreddits": new_list})
    finally:
        db.close()


def _brand_needs_enrichment(brand):
    """True when a brand has no enrichment yet — no `enriched_at` AND no GEO fields.
    Brands the user partly filled by hand (any GEO field present) are left alone, and
    the manual `context` is never touched by enrichment."""
    if not brand:
        return False
    if (brand.get("enriched_at") or "").strip():
        return False
    def _has(v):
        return bool(v) and str(v).strip() not in ("", "[]", "null", "{}")
    return not any(_has(brand.get(k)) for k in
                   ("category", "use_cases", "pain_points", "competitors"))


@app.route("/api/live-posts/generate", methods=["POST"])
def api_live_posts_generate():
    """Generate AI-focused posts for a brand, grounded in brand context.

    Body: {
      brand_id,
      subreddit_name?,                  # optional — omit to generate into the
                                        #   'unassigned' pool and assign later
      intent_counts?: {commercial, comparison, informational},  # flexible sizing
      count?: 3|6|9,                    # legacy 1:1:1 sizing (if no intent_counts)
      force?: bool                      # skip the subreddit-fit guardrail
    }
    - With a subreddit: auto-provisions its row, runs the fit guardrail
      (skip with force), and dedups against live Reddit.
    - Without a subreddit: generates purely from the brand's context
      (covering all offerings) into the 'unassigned' pool; the operator
      assigns a real subreddit per post afterward.
    """
    from config import POST_BATCH_SIZES, INTENT_TYPES
    data = request.json or {}
    bid = data.get("brand_id")
    sub_name = _normalize_sub_name(data.get("subreddit_name", "")) if data.get("subreddit_name") else ""
    force = bool(data.get("force", False))
    # Optional seed: expand generation AROUND an existing prompt / several /
    # a keyword/platform. None when blank → normal brand-context generation.
    seed = (data.get("seed") or "").strip() or None
    # AI-Search semantic-coverage mode (separate option; default off).
    ai_search = bool(data.get("ai_search", False))
    # Optional pasted REAL fan-out queries (captured from ChatGPT/Perplexity/Gemini)
    # — folded into the cluster with region-dedup before generation.
    observed_queries = [str(q).strip() for q in (data.get("observed_queries") or []) if str(q).strip()]
    # Explicit rewrite selection from the Clusters view — generate only for these.
    target_rewrites = [str(q).strip() for q in (data.get("target_rewrites") or []) if str(q).strip()]
    if not bid:
        return jsonify({"error": "brand_id required"}), 400

    # Sizing: explicit per-intent counts take priority over legacy `count`.
    raw_ic = data.get("intent_counts") or None
    intent_counts = None
    count = None
    if raw_ic:
        if not isinstance(raw_ic, dict):
            return jsonify({"error": "intent_counts must be an object"}), 400
        intent_counts = {}
        total = 0
        for it in INTENT_TYPES:
            try:
                n = int(raw_ic.get(it, 0) or 0)
            except (TypeError, ValueError):
                n = 0
            if n < 0:
                n = 0
            if n > 10:
                return jsonify({"error": f"max 10 posts per intent (got {n} for {it})"}), 400
            intent_counts[it] = n
            total += n
        if total < 1:
            return jsonify({"error": "request at least one post across the intents"}), 400
    else:
        count = int(data.get("count", 3))
        if count not in POST_BATCH_SIZES:
            return jsonify({"error": f"count must be one of {list(POST_BATCH_SIZES)}"}), 400

    context_only = not sub_name

    db_check = get_db()
    try:
        brand = db_check.get_brand(bid)
        if not brand:
            return jsonify({"error": "Brand not found"}), 404
        if sub_name and sub_name not in _brand_search_subs(brand):
            return jsonify({
                "error": f"r/{sub_name} is not in this brand's saved subreddits. "
                         "Add it via Live Search → Save to brand or the Edit Brand modal."
            }), 400
    finally:
        db_check.close()

    # Subreddit-fit guardrail (uses the proxy + Claude, cached 24h).
    # Only relevant when a specific subreddit was chosen.
    if sub_name and not force:
        fit = _check_subreddit_fit(brand, sub_name)
        if not fit.get("fits"):
            return jsonify({
                "error": "subreddit_unfit",
                "reason": fit.get("reason") or "This subreddit doesn't fit this brand.",
                "sample_titles": fit.get("sample_titles") or [],
            }), 409

    def task():
        db, claude, _, post_gen, _ = make_generators()
        try:
            if sub_name:
                sub = db.ensure_live_subreddit(sub_name)
                if not sub:
                    raise ValueError(f"Could not provision r/{sub_name}")
            else:
                sub = db.ensure_unassigned_subreddit()
                if not sub:
                    raise ValueError("Could not provision the unassigned pool")
            brand_full = db.get_brand(bid)
            if not brand_full:
                raise ValueError("Brand vanished")

            # Self-heal: a brand added with only a MANUAL context (not enriched) has no
            # use_cases/pain_points/competitors, so first-time coverage + the AI fan-out
            # have nothing to work with. Enrich the GEO fields now — but NEVER overwrite
            # the user's manual `context` (we discard the draft's context_summary).
            if _brand_needs_enrichment(brand_full):
                try:
                    from generators.brand_enrichment import enrich_brand
                    from datetime import datetime as _dt_now
                    draft = enrich_brand(claude, brand_full.get("name") or "",
                                         brand_full.get("domain_url") or "")
                    if draft:
                        db.update_brand(
                            bid,
                            category=(draft.get("category") or None),
                            audience=(draft.get("audience") or None),
                            use_cases=json.dumps(draft.get("use_cases") or []),
                            pain_points=json.dumps(draft.get("pain_points") or []),
                            features=json.dumps(draft.get("features") or []),
                            competitors=json.dumps(draft.get("competitors") or []),
                            enriched_at=_dt_now.now().strftime("%Y-%m-%d %H:%M:%S"),
                        )  # context intentionally omitted → manual context preserved
                        brand_full = db.get_brand(bid)
                        print(f"[live-posts] auto-enriched brand {bid} on generate (manual context kept)")
                except Exception as e:
                    print(f"[live-posts] auto-enrich skipped for brand {bid}: {e}")

            # Generate the batch (per-intent counts or legacy 1:1:1).
            posts = post_gen.generate_posts(
                sub, [brand_full], count=count,
                intent_counts=intent_counts, context_only=context_only,
                seed=seed, ai_search=ai_search, observed_queries=observed_queries,
                target_rewrites=target_rewrites)

            # Live-Reddit dedup pass is now informational only — we no
            # longer split into kept/skipped because duplicate titles
            # across (and even within) subreddits are explicitly
            # allowed. Every generated post is kept; if a live-Reddit
            # match exists we still attach `matched_existing` so the UI
            # can surface a soft hint, but nothing gets dropped.
            skipped = []
            kept = []
            for p in posts:
                # Live-Reddit dedup only makes sense once a subreddit is
                # chosen; pool/context-only batches skip it.
                hit = None
                if sub_name:
                    try:
                        hit = _live_reddit_dup(sub_name, p["title"])
                    except Exception:
                        hit = None
                if hit:
                    kept.append({**p, "matched_existing": hit})
                else:
                    kept.append(p)
            return {
                "subreddit_id": sub["id"],
                "subreddit_name": sub["name"],
                "kept": [
                    {"id": p["id"], "title": p["title"], "intent": p.get("intent"),
                     "storyline": p.get("storyline"),
                     # Optional soft-warning payload — populated only
                     # when the live-Reddit dedup pass found a match.
                     # UI may show a small hint but should NOT skip.
                     **({"matched_existing": p["matched_existing"]}
                        if p.get("matched_existing") else {})}
                    for p in kept
                ],
                # Always empty now (duplicates are no longer skipped);
                # kept in the response shape for backward compat with
                # the UI's render code.
                "skipped": [],
                # AI-Search cluster-completion summary (None unless gap-filling
                # against a persisted cluster for a seeded AI-Search run).
                "coverage": getattr(post_gen, "last_coverage", None),
            }
        finally:
            db.close()

    tid = start_task("live-posts", task)
    return jsonify({"task_id": tid})


@app.route("/api/live-posts/clusters")
def api_live_posts_clusters():
    """Cluster coverage view: each persisted AI-Search cluster (root prompt) →
    its fanned rewrites (covered/gap) → the posts under each. Read-only."""
    bid = request.args.get("brand_id", type=int)
    db = get_db()
    try:
        clusters = db.get_ai_search_clusters_for_brand(bid)
        # Per-brand learned_context (anchor grounding), keyed by seed_norm — cached so
        # we parse each brand's JSON once even when it has many clusters.
        _lc_cache = {}
        def _grounding_for(brand_id, seed_norm):
            if brand_id not in _lc_cache:
                br = db.get_brand(brand_id) if brand_id else None
                try:
                    _lc_cache[brand_id] = json.loads((br or {}).get("learned_context") or "{}")
                except (json.JSONDecodeError, TypeError):
                    _lc_cache[brand_id] = {}
                if not isinstance(_lc_cache[brand_id], dict):
                    _lc_cache[brand_id] = {}
            entry = _lc_cache[brand_id].get(seed_norm)
            if isinstance(entry, dict) and (entry.get("summary") or "").strip():
                return {"summary": entry.get("summary", "").strip(),
                        "covers": bool(entry.get("covers"))}
            return None
        out = []
        for cl in clusters:
            rewrites = db.normalize_rewrites(cl.get("rewrites_json"))
            seed_norm = cl.get("seed_norm")
            posts = db.get_ai_search_posts_for_seed(cl.get("brand_id"), seed_norm)
            # Credit each post to its rewrite by STABLE IDENTITY, not by re-matching text.
            # Posts generated for a region carry ai_search_meta.region (set at production
            # time) → join on the region label (exact, unique within a cluster). Fall back
            # to an exact query match, then to the tolerant matcher ONLY for legacy posts
            # that predate the region stamp. This is why a post never drifts onto a
            # textually-similar sibling region (the "concrete tools" problem).
            rewrite_queries = [r["query"] for r in rewrites]
            region_to_q = {}
            for r in rewrites:
                reg = (r.get("region") or "").strip().lower()
                if reg and reg not in ("(unsorted)", "(from posts)") and reg not in region_to_q:
                    region_to_q[reg] = r["query"].strip().lower()
            q_set = {q.strip().lower() for q in rewrite_queries}
            by_rw = {}
            for p in posts:
                key = None
                p_region = (p.get("region") or "").strip().lower()
                p_tq = (p.get("target_query") or "").strip().lower()
                if p_region and p_region in region_to_q:           # 1) stable region id
                    key = region_to_q[p_region]
                elif p_tq and p_tq in q_set:                       # 2) exact query
                    key = p_tq
                else:                                              # 3) legacy fallback
                    canon = db.match_query_to_rewrites(p.get("target_query"), rewrite_queries)
                    key = canon.strip().lower() if canon else None
                if key is None:
                    continue
                by_rw.setdefault(key, []).append(p)
            rw_rows, covered = [], 0
            for r in rewrites:
                q = r["query"]
                ps = by_rw.get(q.strip().lower(), [])
                if ps:
                    covered += 1
                rw_rows.append({"rewrite": q, "region": r.get("region") or "(unsorted)",
                                "source": r.get("source") or "generated",
                                "variants": r.get("variants") or [],
                                "persona": r.get("persona") or "",
                                "covered": bool(ps), "posts": ps})
            n = len(rewrites)
            out.append({
                "brand_id": cl.get("brand_id"),
                "seed": cl.get("seed") or cl.get("seed_norm"),
                "anchor": cl.get("anchor"),
                "cluster_size": n,
                "covered_count": covered,
                "gap_count": n - covered,
                "complete": n > 0 and covered >= n,
                "backfilled": bool(cl.get("backfilled")),
                "created_at": cl.get("created_at"),
                "grounding": _grounding_for(cl.get("brand_id"), seed_norm),
                "rewrites": rw_rows,
            })
        return jsonify(out)
    finally:
        db.close()


@app.route("/api/live-posts/clusters/backfill", methods=["POST"])
def api_live_posts_clusters_backfill():
    """Reconstruct cluster rows from previously-generated AI-Search posts so they
    appear in the Clusters view. Body: {brand_id?}. Returns {backfilled: N}."""
    data = request.get_json(silent=True) or {}
    raw = data.get("brand_id")
    try:
        bid = int(raw) if raw not in (None, "", 0) else None
    except (TypeError, ValueError):
        bid = None
    db = get_db()
    try:
        n = db.backfill_clusters_from_posts(bid)
        return jsonify({"backfilled": n})
    finally:
        db.close()


@app.route("/api/live-posts/clusters/create", methods=["POST"])
def api_cluster_create():
    """Create an AI-Search cluster for {brand_id, seed} WITHOUT generating posts
    (fan-out + grounding + personas, persisted). Reuse-only: returns the existing
    cluster unchanged if one already exists for the seed."""
    data = request.get_json(silent=True) or {}
    try:
        brand_id = int(data.get("brand_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "brand_id required"}), 400
    seed = (data.get("seed") or "").strip()
    if not seed:
        return jsonify({"error": "seed required"}), 400
    observed = [str(q).strip() for q in (data.get("observed_queries") or []) if str(q).strip()]
    db, claude, _, post_gen, _ = make_generators()
    try:
        brand = db.get_brand(brand_id)
        if not brand:
            return jsonify({"error": "brand not found"}), 404
        res = post_gen.create_cluster([brand], seed, observed_queries=observed)
        code = 200 if not res.get("error") else 502
        return jsonify(res), code
    finally:
        db.close()


@app.route("/api/live-posts/clusters/add-posts", methods=["POST"])
def api_cluster_add_posts():
    """Manually attach existing posts (by post number) to a cluster.
    Body: {brand_id, seed, post_numbers:[...], region?}. When `region` is given (the
    user ticked exactly one region row), attach ALL posts to THAT region directly —
    no LLM re-classification. Otherwise classify each title into a region."""
    data = request.get_json(silent=True) or {}
    try:
        brand_id = int(data.get("brand_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "brand_id required"}), 400
    seed = data.get("seed") or ""
    region = (data.get("region") or "").strip()
    nums = []
    for x in (data.get("post_numbers") or []):
        try:
            nums.append(int(x))
        except (TypeError, ValueError):
            continue
    if not nums:
        return jsonify({"error": "post_numbers required"}), 400
    db, claude, _, post_gen, _ = make_generators()
    try:
        sn = db.normalize_seed(seed)
        cluster = db.get_ai_search_cluster(brand_id, sn)
        if not cluster:
            return jsonify({"error": "no cluster for this seed — create it first"}), 404
        if region:
            # Deterministic: bind every listed post to the chosen region (no classify).
            region_by_num = {num: region for num in nums}
        else:
            # Classify each attached post's title into a region (reuse the existing region
            # labels) so it covers the right region or forms a new one.
            rewrites = db.normalize_rewrites(cluster.get("rewrites_json"))
            existing_regions = sorted({r["region"] for r in rewrites
                                       if r.get("region") and r["region"] not in ("(unsorted)", "(from posts)")})
            title_by_num = {}
            for num in nums:
                row = db.conn.execute(
                    "SELECT title FROM posts WHERE brand_id IS ? AND post_number = ?", (brand_id, num)
                ).fetchone()
                if row:
                    title_by_num[num] = (row["title"] or "").strip()
            numlist = [n for n in nums if title_by_num.get(n)]
            region_by_num = {}
            if numlist:
                classified = post_gen._classify_regions([title_by_num[n] for n in numlist], existing_regions)
                for i, n in enumerate(numlist):
                    if i < len(classified):
                        region_by_num[n] = classified[i].get("region") or ""
        res = db.attach_posts_to_cluster(brand_id, sn, nums, region_by_num=region_by_num)
        return jsonify(res)
    finally:
        db.close()


@app.route("/api/live-posts/clusters/add-fanout", methods=["POST"])
def api_cluster_add_fanout():
    """Manually add real fan-out queries to a cluster with REGION dedup: each query
    is region-classified; added only if its region isn't already present.
    Body: {brand_id, seed, queries:[...]}. Returns {added:[...], skipped:[...]}."""
    data = request.get_json(silent=True) or {}
    try:
        brand_id = int(data.get("brand_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "brand_id required"}), 400
    seed = data.get("seed") or ""
    queries = [str(q).strip() for q in (data.get("queries") or []) if str(q).strip()]
    if not queries:
        return jsonify({"error": "queries required"}), 400
    db, claude, _, post_gen, _ = make_generators()
    try:
        sn = db.normalize_seed(seed)
        cluster = db.get_ai_search_cluster(brand_id, sn)
        if not cluster:
            return jsonify({"error": "no cluster for this seed — generate it first"}), 404
        rewrites = db.normalize_rewrites(cluster.get("rewrites_json"))
        res = post_gen._merge_observed(rewrites, queries)
        db.upsert_ai_search_cluster(
            brand_id, sn, cluster.get("seed") or sn, cluster.get("anchor"),
            res["rewrites"], json.loads(cluster.get("checklist_json") or "[]"), backfilled=0)
        return jsonify({"added": res["added"], "enriched": res.get("enriched", []),
                        "skipped": res["skipped"], "cluster_size": len(res["rewrites"])})
    finally:
        db.close()


@app.route("/api/live-posts/clusters/delete", methods=["POST"])
def api_cluster_delete():
    """Delete a cluster row (posts are kept). Body: {brand_id, seed}."""
    data = request.get_json(silent=True) or {}
    try:
        brand_id = int(data.get("brand_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "brand_id required"}), 400
    seed = data.get("seed") or ""
    db = get_db()
    try:
        n = db.delete_ai_search_cluster(brand_id, db.normalize_seed(seed))
        return jsonify({"deleted": n})
    finally:
        db.close()


@app.route("/api/live-posts/custom", methods=["POST"])
def api_live_posts_custom():
    """Flesh out one full post from a user-supplied topic for brand × sub.

    Body: {brand_id, subreddit_name, topic, force?: bool}
    """
    data = request.json or {}
    bid = data.get("brand_id")
    sub_name = _normalize_sub_name(data.get("subreddit_name", ""))
    topic = (data.get("topic") or "").strip()
    force = bool(data.get("force", False))
    if not bid or not sub_name or not topic:
        return jsonify({"error": "brand_id, subreddit_name, and topic are required"}), 400

    db_check = get_db()
    try:
        brand = db_check.get_brand(bid)
        if not brand:
            return jsonify({"error": "Brand not found"}), 404
        if sub_name not in _brand_search_subs(brand):
            return jsonify({
                "error": f"r/{sub_name} is not in this brand's saved subreddits."
            }), 400
    finally:
        db_check.close()

    if not force:
        fit = _check_subreddit_fit(brand, sub_name)
        if not fit.get("fits"):
            return jsonify({
                "error": "subreddit_unfit",
                "reason": fit.get("reason") or "This subreddit doesn't fit this brand.",
                "sample_titles": fit.get("sample_titles") or [],
            }), 409

    def task():
        db, claude, _, post_gen, _ = make_generators()
        try:
            sub = db.ensure_live_subreddit(sub_name)
            if not sub:
                raise ValueError(f"Could not provision r/{sub_name}")
            brand_full = db.get_brand(bid)
            if not brand_full:
                raise ValueError("Brand vanished")
            # Scope dedup to THIS subreddit only so the same title
            # can be reused across different subs (valid strategy).
            existing = db.get_post_titles_for_brand_in_subreddit(
                brand_full["name"], sub["id"]
            )
            draft = post_gen.generate_post_from_topic(sub, brand_full, topic, existing)
            if not draft:
                # Bubble up the Claude client's last error so the
                # task result (and the UI toast) says WHY instead of
                # the opaque 'Topic generation failed' — common
                # causes: rate limit, missing API key, JSON parse on
                # the model's response, model returned no body.
                last_err = getattr(claude, "last_error", None)
                if last_err:
                    raise ValueError(f"Topic generation failed: {last_err}")
                raise ValueError("Topic generation failed: LLM returned no usable body — see server logs")

            # Live-Reddit dedup is now informational only — duplicate
            # titles across (and within) subreddits are explicitly
            # allowed, so we never block the save. We still attach the
            # match payload so the UI can show a soft hint if it wants.
            try:
                hit = _live_reddit_dup(sub_name, draft["title"])
            except Exception:
                hit = None

            # Save with is_custom=1
            from config import PROMPT_VERSION
            post_id = db.save_post(
                subreddit_id=sub["id"],
                brand_id=brand_full["id"],
                title=draft["title"],
                body=draft["body"],
                storyline=draft.get("storyline", "question"),
                image_prompt=None,
                image_url=None,
                ai_query_score=draft.get("ai_query_score", 0),
                is_custom=1,
                is_filler=0,
                status="complete",
                suggested_post_day=0,
                prompt_version=PROMPT_VERSION,
                brand_ids=[brand_full["id"]],
                intent=draft.get("intent"),
            )
            return {
                "subreddit_id": sub["id"],
                "subreddit_name": sub["name"],
                "post": {
                    "id": post_id,
                    "title": draft["title"],
                    "body": draft["body"],
                    "intent": draft.get("intent"),
                    "storyline": draft.get("storyline"),
                    # Soft hint only — not a blocker.
                    **({"matched_existing": hit} if hit else {}),
                },
            }
        finally:
            db.close()

    tid = start_task("live-posts-custom", task)
    return jsonify({"task_id": tid})

@app.route("/api/generate/comments", methods=["POST"])
def api_gen_comments():
    data = request.json
    # ai_crawl=True (used by Live Subreddits) tells the generator to
    # produce AI-search-engine-retrievable comments: substantive, packed
    # with the brand's domain vocabulary, with long-tail query phrasings
    # woven in. Default False so the regular Posts page is unchanged.
    ai_crawl = bool(data.get("ai_crawl", False))

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
                    ai_crawl=ai_crawl,
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
                    ai_crawl=ai_crawl,
                )
            return {
                "comments": [{"id": c["id"], "body": c["body"][:100]} for c in comments],
                # Reply-context pull outcome (only attempted for published
                # posts with replies). attempted+count==0 → pull failed.
                "existing_pull": getattr(comment_gen, "last_fetch",
                                         {"attempted": False, "count": 0}),
            }
        finally:
            db.close()

    tid = start_task("comments", task)
    return jsonify({"task_id": tid})

@app.route("/api/generate/hq-comment", methods=["POST"])
def api_gen_hq_comment():
    data = request.json
    # Default True to match the UI checkbox (`lsubs-hq-aicrawl`) which
    # is `checked` by default. The toggle gates both the per-comment
    # AI-CRAWL prompt block AND the HQ-MAIN OVERRIDE on the root —
    # OFF produces a Live-Search-style root comment.
    ai_crawl = bool(data.get("ai_crawl", True))
    num_replies = int(data.get("num_replies", 5))

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
                ai_crawl=ai_crawl,
                num_replies=num_replies,
                # AI-Search-mode posts carry a saved phrasing checklist so the
                # anchor covers the whole query cluster (None for normal posts).
                concept_checklist=post.get("concept_checklist"),
            )
            return [{"id": c["id"], "body": c["body"][:100]} for c in comments]
        finally:
            db.close()

    tid = start_task("hq-comment", task)
    return jsonify({"task_id": tid})


# ---------------------------------------------------------------------------
# Add MORE replies to an existing HQ cluster (per-cluster +Replies button)
# ---------------------------------------------------------------------------
@app.route("/api/comments/<int:cid>/hq/add-replies", methods=["POST"])
def api_hq_add_replies(cid):
    """Append more replies to an existing HQ thread.

    The frontend's per-cluster '+ Replies' button hits this. Reads the
    existing cluster for context so the new replies don't rehash points
    the existing replies already covered.
    """
    data = request.json or {}
    num_replies = int(data.get("num_replies", 3))
    ai_crawl = bool(data.get("ai_crawl", True))  # default on for HQ

    def task():
        db, claude, _, _, comment_gen = make_generators()
        try:
            saved = comment_gen.add_replies_to_hq_cluster(
                cid, num_replies=num_replies, ai_crawl=ai_crawl,
            )
            return [{"id": s["id"], "body": s["body"][:100]} for s in saved]
        finally:
            db.close()

    tid = start_task("hq-add-replies", task)
    return jsonify({"task_id": tid})


# ---------------------------------------------------------------------------
# Add an OP-voice reply to an existing HQ cluster (per-cluster +OP Reply)
# ---------------------------------------------------------------------------
@app.route("/api/comments/<int:cid>/hq/op-reply", methods=["POST"])
def api_hq_op_reply(cid):
    """Generate an OP reply that engages with the existing HQ cluster.

    Reads the root + every existing reply (and the LIVE thread when deployed),
    then writes a single OP-voice reply parented to the root.

    Body: {ai_crawl?, affirm_brand?}
      - affirm_brand=False (default): neutral OP reply, never mentions brands.
      - affirm_brand=True: OP returns and endorses the brand as their own
        outcome (for refreshing a live thread).
    """
    data = request.json or {}
    ai_crawl = bool(data.get("ai_crawl", True))
    affirm_brand = bool(data.get("affirm_brand", False))

    def task():
        db, claude, _, _, comment_gen = make_generators()
        try:
            saved = comment_gen.generate_op_reply_to_cluster(
                cid, ai_crawl=ai_crawl, affirm_brand=affirm_brand,
            )
            if saved is None:
                return []
            return [{"id": saved["id"], "body": saved["body"][:100]}]
        finally:
            db.close()

    tid = start_task("hq-op-reply", task)
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

        result = comment_gen._generate_with_validation(
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
            post_intent=post.get("intent"),
            ai_crawl=bool(data.get("ai_crawl", False)),
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
            return {
                "comments": [{"id": c["id"], "body": c["body"][:100]} for c in comments],
                # Whether we managed to pull the post's existing comments
                # for reply context. attempted+count==0 → pull failed.
                "existing_pull": getattr(comment_gen, "last_fetch",
                                         {"attempted": False, "count": 0}),
            }
        finally:
            db.close()

    tid = start_task("live-comments", task)
    return jsonify({"task_id": tid})

# ---------------------------------------------------------------------------
# API: Task polling
# ---------------------------------------------------------------------------

@app.route("/api/tasks/running")
def api_running_tasks():
    db = get_db()
    try:
        return jsonify(db.get_running_tasks())
    finally:
        db.close()

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


@app.route("/api/check-live/debug-comment", methods=["GET"])
def api_check_live_debug_comment():
    """Trace exactly what the comment-liveness RSS check does for a
    given comment URL, through the configured proxy. Tries TWO RSS
    methods and reports raw status/kind/entries for each so we can see
    which works in production (the proxy routes to old.reddit.com,
    which may serve comment RSS differently than www.reddit.com).

    ?url=<full comment URL>   (required)
    """
    import requests as _rq
    import xml.etree.ElementTree as _ET
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "?url=<comment url> required"}), 400
    proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
    share_trace = {}
    resolved = url
    if "/s/" in url:
        # Trace every /s/ resolution strategy through the worker so we
        # can see which one actually expands the share link.
        import requests as _rq2, re as _re2
        s_path = _re2.sub(r'^https?://[^/]+', '', url)
        base_w = proxy.rstrip("/") if proxy else ""
        def _r(label, fn):
            try:
                share_trace[label] = fn()
            except Exception as e:
                share_trace[label] = f"err: {type(e).__name__}: {e}"
        if base_w:
            def _via_resolve():
                r = _rq2.get(f"{base_w}/resolve{s_path}", timeout=20,
                             proxies={"http": None, "https": None})
                body = (r.text or "")[:120].replace("\n", " ")
                try:
                    j = r.json(); u2 = (j or {}).get("url", "")
                except Exception:
                    u2 = ""
                return {"status": r.status_code, "url_field": u2, "body": body}
            _r("worker_resolve", _via_resolve)
            def _via_redirect():
                r = _rq2.get(f"{base_w}{s_path}", timeout=20, allow_redirects=True,
                             proxies={"http": None, "https": None})
                return {"status": r.status_code, "final_url": (r.url or "")}
            _r("worker_redirect", _via_redirect)
            def _via_json():
                r = _rq2.get(f"{base_w}{s_path}.json", timeout=20,
                             proxies={"http": None, "https": None})
                kind = "json" if (r.text or "").lstrip()[:1] in "[{" else "non-json"
                return {"status": r.status_code, "kind": kind,
                        "body": (r.text or "")[:120].replace("\n", " ")}
            _r("worker_json", _via_json)
        try:
            rr = _resolve_reddit_share_url_proxy(url)
            if rr and "/comments/" in rr:
                resolved = rr
        except Exception as e:
            resolved = f"(resolve failed: {e}) {url}"
    m = re.search(r"/r/([^/]+)/comments/([a-z0-9]+)(?:/[^/]*)*?/([a-z0-9]{4,12})/?(?:\?|$)",
                  resolved, re.IGNORECASE)
    parsed = None
    if m:
        parsed = {"sub": m.group(1), "post_id": m.group(2), "comment_id": m.group(3).lower()}
    proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
    base = proxy.rstrip("/") if proxy else "https://www.reddit.com"
    headers = {"User-Agent": REDDIT_USER_AGENT,
               "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.5"}
    NP = {"http": None, "https": None}

    def _redact(u):
        # Never echo embedded credentials (user:pass@host) in output.
        return re.sub(r"://[^/@]+@", "://***:***@", u or "")

    out = {"input": url, "resolved": resolved, "parsed": parsed,
           "proxy_base": _redact(base),
           "share_resolution": {k: ({**v, "url_field": _redact(v.get("url_field", "")),
                                      "final_url": _redact(v.get("final_url", ""))}
                                     if isinstance(v, dict) else v)
                                for k, v in share_trace.items()},
           "methods": {}}

    def classify(t):
        s = (t or "").lstrip()
        return "xml" if s[:5].lower().startswith("<?xml") else ("html" if s[:1] == "<" else ("empty" if not s else "other"))

    if parsed:
        sub, pid, cid = parsed["sub"], parsed["post_id"], parsed["comment_id"]
        for label, rss in [
            ("permalink_rss", f"{base}/r/{sub}/comments/{pid}/comment/{cid}/.rss"),
            ("postlevel_rss", f"{base}/r/{sub}/comments/{pid}/.rss"),
        ]:
            try:
                r = _rq.get(rss, headers=headers, timeout=15, proxies=NP)
                kind = classify(r.text)
                ids, present = [], False
                if kind == "xml":
                    try:
                        root = _ET.fromstring(r.text)
                        ns = {"atom": "http://www.w3.org/2005/Atom"}
                        for e in root.findall("atom:entry", ns):
                            eid = (e.find("atom:id", ns).text or "")
                            ids.append(eid.split(":")[-1][:14])
                            if cid in eid.lower():
                                present = True
                    except Exception as pe:
                        ids = [f"parse_err:{pe}"]
                out["methods"][label] = {
                    "url": _redact(rss), "status": r.status_code, "kind": kind,
                    "entries": len(ids), "comment_id_present": present,
                    "entry_ids": ids[:8],
                    "preview": (r.text[:100].replace("\n", " ") if kind != "xml" else ""),
                }
            except Exception as e:
                out["methods"][label] = {"url": _redact(rss), "error": f"{type(e).__name__}: {e}"}
    out["live_verdict"] = _comment_liveness_via_rss(resolved)
    return jsonify(out)


@app.route("/api/health/rss", methods=["GET"])
def api_health_rss():
    """Lightweight RSS health check. Probes the two RSS feeds the bot
    now depends on (post-listing RSS + comment-thread RSS), through the
    same proxy the bot uses, and reports whether each still returns
    Atom XML.

    Early-warning for the one real long-term risk: Reddit silently
    changing/removing RSS. The admin UI calls this on load (throttled)
    and warns if either feed degrades — so an RSS break surfaces within
    a day instead of silently zeroing out Live Search / Check Live.

    Returns {ok: bool, checks: {posts_rss, comment_rss}}.
    """
    import requests as _rq
    import re as _re
    proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
    base = proxy.rstrip("/") if proxy else "https://www.reddit.com"
    headers = {
        "User-Agent": REDDIT_USER_AGENT,
        "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.5",
    }

    def is_xml(t):
        return bool(t) and t.lstrip()[:5].lower().startswith("<?xml")

    checks = {}
    # 1. Post-listing RSS (drives Live Search).
    try:
        r = _rq.get(f"{base}/r/AskReddit/new.rss", params={"limit": 3},
                    headers=headers, timeout=15)
        ok = r.status_code == 200 and is_xml(r.text)
        checks["posts_rss"] = {"ok": ok, "status": r.status_code,
                               "kind": "xml" if is_xml(r.text) else "non-xml"}
        # Derive a real post id for the comment-RSS probe.
        post_id = None
        if ok:
            m = _re.search(r"/comments/([a-z0-9]+)/", r.text)
            post_id = m.group(1) if m else None
    except Exception as e:
        checks["posts_rss"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        post_id = None

    # 2. Comment-thread RSS (drives Check Live + gen-comment context).
    try:
        # Use the derived post id, or a fallback path that still
        # exercises the comment-RSS route.
        cu = (f"{base}/r/AskReddit/comments/{post_id}/.rss" if post_id
              else f"{base}/r/AskReddit/new.rss")
        r2 = _rq.get(cu, params={"limit": 3}, headers=headers, timeout=15)
        ok2 = r2.status_code == 200 and is_xml(r2.text)
        checks["comment_rss"] = {"ok": ok2, "status": r2.status_code,
                                 "kind": "xml" if is_xml(r2.text) else "non-xml"}
    except Exception as e:
        checks["comment_rss"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    overall = all(c.get("ok") for c in checks.values())
    return jsonify({"ok": overall, "proxy_configured": bool(proxy), "checks": checks})


@app.route("/api/search/reddit/test-http-proxy", methods=["GET"])
def api_search_reddit_test_http_proxy():
    """Verify the residential HTTP proxy (REDDIT_HTTP_PROXY) actually
    unblocks Reddit's JSON API. Hits 3 JSON endpoints through the
    proxy and reports status / body-kind / preview, plus the egress
    IP the proxy presents (via httpbin) so you can confirm it's
    residential and differs from Railway's own IP.

    Run this AFTER setting REDDIT_HTTP_PROXY on Railway. If the JSON
    probes come back body_kind=json, the proxy works and I can flip
    comment-fetching + the search leg back to JSON.

    ?sub=Mattress&q=mattress  (optional test targets)
    """
    import requests as _rq
    proxies = _reddit_http_proxies()
    if not proxies:
        return jsonify({"error": "REDDIT_HTTP_PROXY is not set on this server"}), 400
    sub = (request.args.get("sub") or "Mattress").strip()
    q = (request.args.get("q") or "mattress").strip()
    ua = REDDIT_USER_AGENT

    def classify(text):
        s = (text or "").lstrip()
        if not s:
            return "empty"
        if s[0] in "{[":
            return "json"
        if s[:6].lower().startswith("<?xml"):
            return "xml"
        if s[0] == "<":
            return "html"
        return "other"

    # A realistic desktop-Chrome header set. Reddit's anti-bot
    # challenge keys off UA + sec-fetch/sec-ch-ua headers, not just IP —
    # a bot UA gets the 403 challenge page even from a clean residential
    # IP. These mimic a real browser navigation.
    BROWSER_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Referer": f"https://www.reddit.com/r/{sub}/",
    }

    def probe(url, params=None, headers=None, retries=1):
        last = None
        for _ in range(retries):
            try:
                r = _rq.get(url, params=params, proxies=proxies, timeout=25,
                            headers=headers or {"User-Agent": ua, "Accept": "application/json"})
                body = r.text or ""
                last = {
                    "status": r.status_code,
                    "content_type": r.headers.get("Content-Type", ""),
                    "body_kind": classify(body),
                    "len": len(body),
                    "preview": body[:120].replace("\n", " "),
                }
                if last["body_kind"] in ("json", "xml"):
                    return last  # success — stop retrying
            except Exception as e:
                last = {"error": f"{type(e).__name__}: {e}"}
        return last

    out = {
        # Original bot-UA probe (baseline — expected to 403).
        "botUA_www_json": probe(f"https://www.reddit.com/r/{sub}.json", {"limit": 3}),
        # Browser-headers probes — the combo that usually clears the
        # anti-bot challenge on a residential IP.
        "browser_www_json_listing": probe(f"https://www.reddit.com/r/{sub}.json",
                                          {"limit": 3, "raw_json": 1}, headers=BROWSER_HEADERS),
        "browser_www_json_search": probe(f"https://www.reddit.com/r/{sub}/search.json",
                                         {"q": q, "restrict_sr": "on", "limit": 3, "raw_json": 1},
                                         headers=BROWSER_HEADERS),
        "browser_old_json_listing": probe(f"https://old.reddit.com/r/{sub}.json",
                                          {"limit": 3, "raw_json": 1}, headers=BROWSER_HEADERS),
        # Retry 4× to ride IPRoyal's IP rotation — a burned IP this
        # request may be a clean one next request.
        "browser_www_json_retry4": probe(f"https://www.reddit.com/r/{sub}.json",
                                         {"limit": 3, "raw_json": 1},
                                         headers=BROWSER_HEADERS, retries=4),
        # RSS through residential (parity check — should at least work).
        "rss_www_new": probe(f"https://www.reddit.com/r/{sub}/new.rss", {"limit": 3},
                             headers=BROWSER_HEADERS),
        "proxy_egress_ip": probe("https://httpbin.org/ip"),
    }
    return jsonify({
        "proxy_active": True,
        "note": ("Looking for ANY browser_* probe with body_kind=json. "
                 "If found, residential+browser-headers unblocks JSON and "
                 "I'll wire the bot to use that exact combo. botUA_* is the "
                 "baseline (bot UA, expected 403)."),
        "probes": out,
    })


@app.route("/api/search/reddit/raw-probe", methods=["GET"])
def api_search_reddit_raw_probe():
    """Ground-truth probe: hit every Reddit endpoint DIRECTLY from
    this server's egress IP (no bot cascade, no fallbacks) and report
    exactly what comes back — status code, content-type, whether the
    body is JSON / XML / HTML, and a short preview.

    Reddit's bot detection is IP-sensitive: an endpoint that 403s from
    one network may serve clean JSON from another. This tells us what
    the PRODUCTION IP actually gets, instead of guessing from a dev
    sandbox.

    Query params:
      ?sub=Mattress   (subreddit to test against; default Mattress)
      ?q=mattress     (search keyword; default mattress)
    """
    import requests as _rq
    sub = (request.args.get("sub") or "Mattress").strip()
    q = (request.args.get("q") or "mattress").strip()

    # UAs to try per endpoint — Reddit allow-lists vary by UA shape.
    uas = {
        "script": "python:reddit-strategy:v1 (by /u/strategy_bot_admin)",
        "browser": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "configured": REDDIT_USER_AGENT,
    }

    # (label, url, params) — covers JSON, RSS, and OAuth-less probes.
    targets = [
        ("www_json_search", f"https://www.reddit.com/r/{sub}/search.json",
         {"q": q, "restrict_sr": "on", "limit": 3}),
        ("old_json_search", f"https://old.reddit.com/r/{sub}/search.json",
         {"q": q, "restrict_sr": "on", "limit": 3}),
        ("www_json_listing", f"https://www.reddit.com/r/{sub}.json", {"limit": 3}),
        ("www_json_new", f"https://www.reddit.com/r/{sub}/new.json", {"limit": 3}),
        ("www_rss_search", f"https://www.reddit.com/r/{sub}/search.rss",
         {"q": q, "restrict_sr": "on", "limit": 3}),
        ("www_rss_new", f"https://www.reddit.com/r/{sub}/new.rss", {"limit": 3}),
        ("proxy_json_search", None, {"q": q, "restrict_sr": "on", "limit": 3}),
    ]

    def classify(text):
        s = (text or "").lstrip()
        if not s:
            return "empty"
        if s[0] in "{[":
            return "json"
        if s.startswith("<?xml") or s[:20].lower().startswith("<?xml"):
            return "xml"
        if s[0] == "<":
            return "html"
        return "other"

    results = {}
    proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
    for label, url, params in targets:
        if label == "proxy_json_search":
            if not proxy:
                results[label] = {"skipped": "no REDDIT_PROXY_URL configured"}
                continue
            url = f"{proxy.rstrip('/')}/r/{sub}/search.json"
        per_ua = {}
        for ua_label, ua in uas.items():
            try:
                r = _rq.get(url, params=params,
                            headers={"User-Agent": ua, "Accept": "*/*"},
                            timeout=12)
                body = r.text or ""
                per_ua[ua_label] = {
                    "status": r.status_code,
                    "content_type": r.headers.get("Content-Type", ""),
                    "body_kind": classify(body),
                    "len": len(body),
                    "preview": body[:120].replace("\n", " "),
                }
            except Exception as e:
                per_ua[ua_label] = {"error": f"{type(e).__name__}: {e}"}
        results[label] = per_ua

    return jsonify({
        "sub": sub, "q": q,
        "proxy_configured": bool(proxy),
        "egress_note": "These results reflect THIS server's IP, not a dev sandbox.",
        "targets": results,
    })


@app.route("/api/search/reddit/proxy-health", methods=["GET"])
def api_search_reddit_proxy_health():
    """Distinguish 'proxy IP is blocked by Reddit' from 'proxy logic is
    broken' from 'proxy egress IP is fine but my Railway IP is blocked'.

    Runs four probes and reports each:
      1. proxy → a NON-Reddit URL (httpbin /ip) — proves the proxy
         forwards at all AND reveals the proxy's egress IP.
      2. proxy → Reddit RSS new feed.
      3. proxy → Reddit JSON listing.
      4. direct (no proxy) → httpbin /ip — reveals THIS server's IP
         for comparison.

    Interpreting results:
      - If probe 1 succeeds (kind=json, shows an IP) but 2 & 3 return
        Reddit's block HTML → the proxy WORKS, but its egress IP is on
        Reddit's block list. A different proxy IP (residential) would
        fix it.
      - If probe 1 fails too → the proxy itself is misconfigured.
      - Compare the IP in probe 1 (proxy egress) vs probe 4 (Railway
        egress): if they differ, the proxy is routing through a
        different IP — useful to know which one Reddit is blocking.
    """
    import requests as _rq
    proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
    if not proxy:
        return jsonify({"error": "No REDDIT_PROXY_URL configured"}), 400
    base = proxy.rstrip("/")

    def classify(text):
        s = (text or "").lstrip()
        if not s:
            return "empty"
        if s[0] in "{[":
            return "json"
        if s[:6].lower().startswith("<?xml"):
            return "xml"
        if s[0] == "<":
            return "html"
        return "other"

    def probe(url, **kw):
        try:
            r = _rq.get(url, timeout=12,
                        headers={"User-Agent": "python:reddit-strategy:v1 (proxy-health)"},
                        **kw)
            body = r.text or ""
            return {
                "status": r.status_code,
                "content_type": r.headers.get("Content-Type", ""),
                "body_kind": classify(body),
                "preview": body[:160].replace("\n", " "),
            }
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    out = {}
    # 1. Can the proxy reach a non-Reddit URL? Most proxies that just
    #    rewrite the path to reddit.com won't forward arbitrary hosts,
    #    so this may 404 — that's fine, it still tells us the worker
    #    is alive. We also try the worker root.
    out["proxy_root"] = probe(base + "/")
    # 2. proxy → Reddit RSS
    out["proxy_rss_new"] = probe(base + "/r/Mattress/new.rss", params={"limit": 3})
    # 3. proxy → Reddit JSON listing
    out["proxy_json_listing"] = probe(base + "/r/Mattress.json", params={"limit": 3})
    # 4. Direct from this server → httpbin to reveal Railway egress IP
    out["direct_httpbin_ip"] = probe("https://httpbin.org/ip")

    return jsonify({
        "proxy_url": base,
        "note": ("If proxy_rss_new / proxy_json_listing show body_kind=html "
                 "(Reddit block page) the proxy's egress IP is blocked by "
                 "Reddit. A residential-IP proxy would be needed. "
                 "direct_httpbin_ip shows this server's own egress IP."),
        "probes": out,
    })


@app.route("/api/search/reddit/diagnose", methods=["GET"])
def api_search_reddit_diagnose():
    """Diagnostic: run a single-keyword search through each API leg
    (reddit, pullpush, arctic) IN ISOLATION and report per-leg raw
    count + any exception. Strips every filter so the only signal is
    "did this leg return anything from upstream".

    Use this when the regular search returns 0 results to figure out
    which leg(s) are actually broken vs all-of-them-fine-but-filters-
    are-too-tight.

    Query params:
      ?keyword=mattress             (required)
      ?subreddit=Mattress           (optional)
      ?limit=10                     (default 10 — keep small)
    """
    from search.reddit_bot import RedditSearchBot
    keyword = (request.args.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"error": "?keyword=... required"}), 400
    subreddit = (request.args.get("subreddit") or "").strip() or None
    limit = min(int(request.args.get("limit") or 10), 50)

    proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
    bot = RedditSearchBot(
        reddit_base=proxy.rstrip("/") if proxy else None,
        script_user_agent=REDDIT_USER_AGENT,
        retry_attempts=2,  # fail fast for diagnostics
    )

    legs = {}
    for leg in ("reddit", "pullpush", "arctic"):
        try:
            # Bypass scrutiny / subscriber filters — we want raw API
            # output, not filtered. Also drop the date window so a
            # leg that paginates time-based doesn't return empty
            # because of a too-narrow window.
            results = bot.search(
                keyword=keyword,
                subreddit=subreddit,
                sort_by="relevance",
                limit=limit,
                api=leg,
            )
            legs[leg] = {
                "ok": True,
                "count": len(results),
                "sample_titles": [r.get("title", "")[:80] for r in results[:3]],
            }
        except Exception as e:
            legs[leg] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return jsonify({
        "keyword": keyword,
        "subreddit": subreddit,
        "proxy_set": bool(proxy),
        "user_agent": REDDIT_USER_AGENT,
        "legs": legs,
    })


@app.route("/api/search/reddit/debug-search", methods=["GET"])
def api_search_reddit_debug_search():
    """Read-only: run the multi-keyword × multi-sub search and return the
    raw results (title + subreddit + which keyword matched) WITHOUT
    saving — so we can see whether the search is returning on-topic posts
    from the right subreddits, vs off-topic / wrong-sub noise.

    ?keywords=payroll,hris,hr & subs=Payroll,humanresources,Accounting
    & days=7 & limit=30
    """
    from search.reddit_bot import RedditSearchBot, balance_posts_by_subreddit
    keywords = [k.strip() for k in (request.args.get("keywords") or "").split(",") if k.strip()]
    subs = [s.strip() for s in (request.args.get("subs") or "").split(",") if s.strip()]
    days = int(request.args.get("days") or 7)
    limit = min(int(request.args.get("limit") or 30), 100)
    if not keywords:
        return jsonify({"error": "?keywords=a,b,c required"}), 400

    proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
    bot = RedditSearchBot(reddit_base=proxy.rstrip("/") if proxy else None,
                          script_user_agent=REDDIT_USER_AGENT)
    try:
        if len(keywords) > 1:
            results = bot.search_multiple_keywords(
                keywords, concurrent=True, subreddits=subs or None,
                sort_by="relevance", max_days_old=days, limit=limit, api="auto",
            )
        else:
            results = bot.search(
                keywords[0], subreddits=subs or None,
                sort_by="relevance", max_days_old=days, limit=limit, api="auto",
            )
        results = balance_posts_by_subreddit(results, limit, subs or None)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    # Which requested keyword does each result's title/body actually
    # contain (word-boundary)? Helps spot search returning posts that
    # don't match any keyword (= search relevance problem).
    import re as _re
    kw_pats = [(k, _re.compile(r"\b" + _re.escape(k.lower()) + r"\b")) for k in keywords]
    requested_subs_lc = {s.lower() for s in subs}
    rows = []
    off_sub = 0
    no_kw = 0
    for r in results:
        text = (r.get("title", "") + " " + (r.get("text", "") or "")).lower()
        hits = [k for k, p in kw_pats if p.search(text)]
        rsub = (r.get("subreddit") or "")
        in_requested = (not requested_subs_lc) or (rsub.lower() in requested_subs_lc)
        if not in_requested:
            off_sub += 1
        if not hits:
            no_kw += 1
        rows.append({
            "subreddit": rsub,
            "in_requested_subs": in_requested,
            "title": (r.get("title") or "")[:90],
            "keyword_hits": hits,
            "comments": r.get("comments"),
            "score": r.get("score"),
            "source_hint": r.get("_source", "json/pullpush/arctic"),
        })
    from collections import Counter
    return jsonify({
        "keywords": keywords, "subs": subs, "days": days,
        "total_results": len(rows),
        "results_from_wrong_subreddit": off_sub,
        "results_matching_no_keyword": no_kw,
        "by_subreddit": dict(Counter(r["subreddit"] for r in rows)),
        "results": rows,
    })


@app.route("/api/search/reddit", methods=["POST"])
def api_search_reddit():
    """Run a Reddit keyword search via RedditSearchBot (background task).

    Accepts either a manual `keyword` string OR an `auto_brand` dict that
    triggers multi-keyword search with keywords auto-generated from a brand's
    name/context (saved or ad-hoc).
    """
    from search.reddit_bot import RedditSearchBot, balance_posts_by_subreddit
    import math
    data = request.json or {}
    keyword = (data.get("keyword") or "").strip()
    keywords_list = data.get("keywords") or []  # comma-separated multi-keyword support
    auto_brand = data.get("auto_brand")
    if not keyword and not keywords_list and not auto_brand:
        return jsonify({"error": "keyword, keywords, or auto_brand is required"}), 400

    def task():
        proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
        # Pass the project's script-style UA so Reddit treats the
        # search bot identically to every other Reddit call in the
        # app — _reddit_get uses the same UA. Browser-mimicking UAs
        # (Chrome) trigger HTML challenge pages much more often.
        bot = RedditSearchBot(
            reddit_base=proxy.rstrip("/") if proxy else None,
            script_user_agent=REDDIT_USER_AGENT,
        )
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

            # Manual multi-keyword path
            if keywords_list:
                n = max(len(keywords_list), 1)
                # Each keyword runs the full cascade with the full requested
                # limit as its budget. Heavy cross-keyword overlap (e.g.
                # "testosterone" and "TRT") shrinks unique results fast, so
                # dividing the budget across keywords starves the output.
                # Dedup + final balance below cap the response at requested_limit.
                per_kw_limit = requested_limit
                print(f"    Multi-keyword search: {n} keywords, per_kw_limit={per_kw_limit}")
                print(f"    Keywords: {keywords_list}")
                merged = bot.search_multiple_keywords(
                    keywords_list, concurrent=True, limit=per_kw_limit, **common_filters
                )
                # Final cross-keyword balance: take up to (limit // N_subs)
                # posts from each requested subreddit first, fill remainder
                # with top-scoring overflow. Without this, merged[:limit]
                # would favor whichever subs have higher-karma posts.
                subs_list = data.get("subreddits")
                trimmed = balance_posts_by_subreddit(merged, requested_limit, subs_list)
                return {"results": trimmed, "generated_keywords": keywords_list}

            # Auto-brand path
            keywords, source = _resolve_brand_keywords(auto_brand, db=task_db)
            n = max(len(keywords), 1)
            per_kw_limit = requested_limit
            print(f"    Auto-brand search: {n} keywords, per_kw_limit={per_kw_limit}, source={source}")
            print(f"    Keywords: {keywords}")
            merged = bot.search_multiple_keywords(
                keywords, concurrent=True, limit=per_kw_limit, **common_filters
            )
            # Final cross-keyword balance — same rationale as the manual path.
            subs_list = data.get("subreddits")
            trimmed = balance_posts_by_subreddit(merged, requested_limit, subs_list)
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
        pid, is_new = db.save_search_post(data)
        if pid is None:
            return jsonify({"error": "Post already saved"}), 409
        return jsonify({"id": pid, "is_new": is_new})
    finally:
        db.close()


@app.route("/api/search/posts/bulk", methods=["POST"])
def api_save_search_posts_bulk():
    db = get_db()
    try:
        items = request.json.get("posts", [])
        new_ids, existing_ids = [], []
        for item in items:
            if not item.get("reddit_url"):
                continue
            pid, is_new = db.save_search_post(item)
            if pid is None:
                continue
            (new_ids if is_new else existing_ids).append(pid)
        return jsonify({
            "saved": len(new_ids),
            "duplicates": len(existing_ids),
            "saved_ids": new_ids,
            "existing_ids": existing_ids,
            "all_ids": new_ids + existing_ids,
        })
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


@app.route("/api/search/posts/cleanup-recent", methods=["POST"])
def api_cleanup_recent_search_posts():
    """Bulk-delete recently-saved, UN-GENERATED Live Search posts across
    ALL brands. Targets search_posts with:
      - created_at within the last `hours` (default 24), AND
      - status IN ('saved','irrelevant')  -- never 'complete'/'generating'

    Destructive + irreversible. Requires {"confirm": true} in the body.
    Pass {"dry_run": true} to preview the count without deleting.
    Uses db.delete_search_post() per row so child search_comments +
    account lifetime counters are cleaned up correctly.

    Body: {confirm: bool, hours?: int (default 24), dry_run?: bool}
    Returns {deleted, matched, by_status, by_brand}.
    """
    data = request.get_json(silent=True) or {}
    hours = int(data.get("hours", 24))
    dry_run = bool(data.get("dry_run", False))
    if not dry_run and not data.get("confirm"):
        return jsonify({"error": "Pass {\"confirm\": true} to delete, or {\"dry_run\": true} to preview."}), 400

    db = get_db()
    try:
        rows = db.conn.execute(
            """SELECT sp.id, sp.status, sp.subreddit,
                      COALESCE(b.name, '(no brand)') AS brand_name
                 FROM search_posts sp
            LEFT JOIN brands b ON sp.brand_id = b.id
                WHERE sp.status IN ('saved', 'irrelevant')
                  AND sp.created_at >= datetime('now', ?)""",
            (f'-{hours} hours',)
        ).fetchall()
        from collections import Counter
        by_status = dict(Counter(r["status"] for r in rows))
        by_brand = dict(Counter(r["brand_name"] for r in rows))
        matched = len(rows)
        deleted = 0
        if not dry_run:
            for r in rows:
                try:
                    db.delete_search_post(r["id"])
                    deleted += 1
                except Exception as e:
                    print(f"[cleanup-recent] failed to delete search_post {r['id']}: {e}", flush=True)
        return jsonify({
            "dry_run": dry_run,
            "hours": hours,
            "matched": matched,
            "deleted": deleted,
            "by_status": by_status,
            "by_brand": by_brand,
        })
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
    """Generate comments for a saved search post (background task).

    Uses the consolidated CommentGenerator (generators/comment_gen.py) so the
    same persona pool, banned-phrase filter, intent classifier, and
    AI-crawl rules apply that we use everywhere else. The legacy
    CommentGeneratorBot has been retired.
    """
    from generators.comment_gen import CommentGenerator
    from generators.base import ClaudeClient
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
    # Pre-consolidation parity: Live Search did not have an ai_crawl mode.
    # Default to False so behaviour matches the legacy bot. Callers can
    # still opt in by passing ai_crawl=True explicitly.
    ai_crawl = bool(data.get("ai_crawl", False))

    def task():
        proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
        api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
        db2 = Database(DB_PATH)
        db2.connect()
        db2.initialize()
        cg = CommentGenerator(
            ClaudeClient(api_key), db2,
            reddit_base=proxy.rstrip("/") if proxy else None,
        )
        try:
            db2.update_search_post_status(pid, "generating")

            # Fetch live Reddit comments from the post
            comments, post_body, is_archived = cg.fetch_comments(post["reddit_url"], limit=20)
            if is_archived:
                db2.update_search_post_status(pid, "saved")
                raise ValueError("Post is archived — cannot comment")
            # No comment-count gate — RSS counts are unreliable, so 0
            # comments is allowed (generation works without existing
            # comments). Only archived posts are blocked above.

            comment_stats = cg._compute_comment_stats(comments)

            # Relevance check
            relevance = cg.check_relevance(
                post["title"], post_body, post["subreddit"],
                comments, brand_name, brand_context, brand_keywords,
            )
            rel_score = relevance.get("score", 0)
            threshold = data.get("relevance_threshold", 6)
            if rel_score < threshold:
                db2.update_search_post_status(pid, "irrelevant")
                return {
                    "skipped": True,
                    "relevance_score": rel_score,
                    "threshold": threshold,
                    "reason": f"Post relevance score ({rel_score}) is below threshold ({threshold}). Comments not generated.",
                }

            # Tone analysis
            tone_analysis = cg.analyze_tone(
                post["title"], post_body, post["subreddit"],
                comments, comment_stats,
            )

            # Reply targets — only when generating ≥3 comments, picks one
            # real Reddit comment for slot index 2 to reply to.
            reply_targets = {}
            if num_comments >= 3:
                reply_target = cg._select_reply_target(
                    comments, post["title"], brand_name, relevance,
                )
                if reply_target:
                    reply_targets[2] = reply_target

            # Live Search wants every comment to mention the brand
            mention_flags = [True] * num_comments
            brand_focus = cg._extract_brand_focus(brand)

            generation = cg.generate_comments(
                post_title=post["title"],
                post_body=post_body,
                subreddit=post["subreddit"],
                comments=comments,
                brand_name=brand_name,
                brand_context=brand_context,
                num_comments=num_comments,
                tone_analysis=tone_analysis,
                comment_stats=comment_stats,
                relevance=relevance,
                reply_targets=reply_targets,
                mention_brand_flags=mention_flags,
                all_brand_names=[brand_name],
                ai_crawl=ai_crawl,
                brand_focus=brand_focus,
            )

            generated = generation.get("generated_comments", []) or []
            if not generated:
                db2.update_search_post_status(pid, "saved")
                raise ValueError("Comment generation failed")

            # Brand-mention enforcement: retry once if any comment misses the brand
            missing = [i + 1 for i, c in enumerate(generated)
                       if brand_name.lower() not in c.lower()]
            if missing:
                feedback = f"Comment(s) {missing} don't mention '{brand_name}'. Naturally weave in a mention."
                retry_gen = cg.generate_comments(
                    post_title=post["title"],
                    post_body=post_body,
                    subreddit=post["subreddit"],
                    comments=comments,
                    brand_name=brand_name,
                    brand_context=brand_context,
                    num_comments=num_comments,
                    tone_analysis=tone_analysis,
                    comment_stats=comment_stats,
                    retry_feedback=feedback,
                    relevance=relevance,
                    reply_targets=reply_targets,
                    mention_brand_flags=mention_flags,
                    all_brand_names=[brand_name],
                    ai_crawl=ai_crawl,
                    brand_focus=brand_focus,
                )
                retry_comments = retry_gen.get("generated_comments", []) or []
                if retry_comments:
                    retry_missing = [i for i, c in enumerate(retry_comments)
                                     if brand_name.lower() not in c.lower()]
                    if len(retry_missing) < len(missing):
                        generated = retry_comments
                        generation = retry_gen

            # Save to search_comments
            personas_meta = generation.get("_personas") or []
            stored = []
            for idx, body in enumerate(generated):
                is_reply = 1 if idx == 2 and reply_targets.get(2) else 0
                reply_url = None
                if is_reply and reply_targets.get(2):
                    rt = reply_targets[2]
                    reply_url = (f"https://www.reddit.com{rt.get('permalink', '')}"
                                 if rt.get('permalink') else None)
                mentions = 1 if brand_name.lower() in body.lower() else 0
                cid = db2.add_search_comment(
                    search_post_id=pid, body=body, brand_id=post.get("brand_id"),
                    persona_id=personas_meta[idx] if idx < len(personas_meta) else None,
                    is_reply=is_reply, reply_to_url=reply_url,
                    mentions_brand=mentions, relevance_score=rel_score,
                )
                stored.append({"id": cid, "body": body[:100]})

            db2.update_search_post_status(pid, "complete")
            return {"generated": len(stored), "comments": stored, "relevance_score": rel_score}
        finally:
            db2.close()

    tid = start_task("generate-search-comments", task)
    return jsonify({"task_id": tid})


def _generate_hq_for_search_post(cg, db2, pid, post, brand, brand_name, brand_context,
                                  brand_keywords, relevance_threshold=6,
                                  ai_crawl=True):
    """Run an HQ thread generation for a single saved search post.

    Shared by /api/search/posts/<pid>/generate-hq and the batch endpoint.
    `cg` is a `CommentGenerator` instance (consolidated from the legacy
    CommentGeneratorBot). Returns a result dict with keys: generated,
    comments (list), relevance_score, or skipped/error fields. Caller is
    responsible for status updates and errors.
    """
    db2.update_search_post_status(pid, "generating")

    comments, post_body, is_archived = cg.fetch_comments(post["reddit_url"], limit=20)
    _fetch = getattr(cg, "last_fetch", {"count": len(comments), "source": "?"})
    if is_archived:
        db2.update_search_post_status(pid, "saved")
        raise ValueError("Post is archived — cannot comment")
    # NOTE: no comment-count gate. RSS comment counts are unreliable
    # (RSS often under-reports / omits the count), so we do NOT skip
    # posts for having "0" comments — generation proceeds regardless
    # (tone analysis falls back to defaults, the HQ main comment is a
    # top-level reply to the post). Only `is_archived` blocks gen.
    comment_stats = cg._compute_comment_stats(comments)

    relevance = cg.check_relevance(
        post["title"], post_body, post["subreddit"],
        comments, brand_name, brand_context, brand_keywords,
    )
    rel_score = relevance.get("score", 0)
    if rel_score < relevance_threshold:
        db2.update_search_post_status(pid, "irrelevant")
        return {
            "skipped": True,
            "relevance_score": rel_score,
            "threshold": relevance_threshold,
            # Diagnostics so the operator can tell a genuine low-relevance
            # skip from one caused by thin comment context.
            "comments_fetched": _fetch.get("count"),
            "comments_source": _fetch.get("source"),
            "relevance_breakdown": {
                k: relevance.get(k) for k in
                ("topic_match", "problem_fit", "natural_fit",
                 "conversation_opening", "disqualified", "disqualify_reason", "reason")
                if k in relevance
            },
            "reason": (f"Post relevance score ({rel_score}) is below threshold "
                       f"({relevance_threshold}). Fetched {_fetch.get('count')} "
                       f"comment(s) via {_fetch.get('source')}."),
        }

    tone_analysis = cg.analyze_tone(
        post["title"], post_body, post["subreddit"], comments, comment_stats,
    )

    hq_results = cg.generate_hq_search_thread(
        post_title=post["title"], post_body=post_body,
        subreddit_name=post["subreddit"],
        comments=comments, brand_name=brand_name, brand_context=brand_context,
        tone_analysis=tone_analysis, comment_stats=comment_stats,
        relevance=relevance, num_replies=4,
        ai_crawl=ai_crawl,
        brand_focus=cg._extract_brand_focus(brand),
    )
    if not hq_results:
        db2.update_search_post_status(pid, "saved")
        raise ValueError("HQ generation failed")

    # Save in the order returned (parents always come before children, since
    # the shape iterates idx 0..4). Map list-idx → DB id so children link to
    # their already-saved parent.
    saved_id_by_idx = {}
    stored = []
    for entry in hq_results:
        idx = entry["idx"]
        parent_idx = entry["parent_idx"]
        is_main = entry["is_main"]
        parent_db_id = saved_id_by_idx.get(parent_idx) if parent_idx is not None else None

        cid = db2.add_search_comment(
            search_post_id=pid,
            body=entry["body"],
            brand_id=post.get("brand_id"),
            persona_id=entry.get("persona_id"),
            is_reply=0 if is_main else 1,
            reply_to_url=None,  # filled at deploy time when parent gets a Reddit URL
            mentions_brand=entry.get("mentions_brand", 0),
            relevance_score=rel_score,
            comment_type="hq",
            parent_comment_id=parent_db_id,
        )
        saved_id_by_idx[idx] = cid
        stored.append({"id": cid, "idx": idx, "parent_idx": parent_idx,
                        "is_main": is_main, "body": entry["body"][:100]})

    db2.update_search_post_status(pid, "complete")
    return {"generated": len(stored), "comments": stored, "relevance_score": rel_score}


@app.route("/api/search/posts/<int:pid>/generate-hq", methods=["POST"])
def api_generate_search_hq(pid):
    """Generate an HQ thread (1 main + 4 replies, possibly nested) for a saved
    search post. Background task. Uses the consolidated CommentGenerator."""
    from generators.comment_gen import CommentGenerator
    from generators.base import ClaudeClient
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
    threshold = data.get("relevance_threshold", 6)
    # Pre-consolidation parity for Live Search HQ — default ai_crawl off.
    ai_crawl = bool(data.get("ai_crawl", False))

    def task():
        proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
        api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
        db2 = Database(DB_PATH)
        db2.connect()
        db2.initialize()
        cg = CommentGenerator(
            ClaudeClient(api_key), db2,
            reddit_base=proxy.rstrip("/") if proxy else None,
        )
        try:
            return _generate_hq_for_search_post(
                cg, db2, pid, post, brand, brand_name, brand_context,
                brand_keywords, relevance_threshold=threshold,
                ai_crawl=ai_crawl,
            )
        finally:
            db2.close()

    tid = start_task("generate-search-hq", task)
    return jsonify({"task_id": tid})


@app.route("/api/search/posts/generate-hq-batch", methods=["POST"])
def api_generate_search_hq_batch():
    """Batch HQ generator: 1 main + 4 replies per post, sequentially in one task.
    Uses the consolidated CommentGenerator."""
    from generators.comment_gen import CommentGenerator
    from generators.base import ClaudeClient
    data = request.json or {}
    post_ids = data.get("post_ids", [])
    threshold = data.get("relevance_threshold", 6)
    # Pre-consolidation parity for Live Search HQ batch — default ai_crawl off.
    ai_crawl = bool(data.get("ai_crawl", False))
    dedup = bool(data.get("dedup", False))
    exclude_subs = {(s or "").strip().lower() for s in (data.get("exclude_subs") or [])}

    if not post_ids:
        return jsonify({"error": "No post_ids provided"}), 400

    db_check = get_db()
    valid_posts = []
    for pid in post_ids:
        post = db_check.get_search_post(pid)
        if not post:
            continue
        if post.get("status") in ("generating", "irrelevant"):
            continue
        brand = db_check.get_brand(post["brand_id"]) if post.get("brand_id") else None
        if not brand:
            continue
        if dedup:
            if (post.get("subreddit") or "").strip().lower() in exclude_subs:
                continue
            if db_check.search_post_has_comments_for_brand(pid, post["brand_id"], hq=True):
                continue
        valid_posts.append({
            "pid": pid, "post": post, "brand": brand,
            "brand_name": brand["name"],
            "brand_context": brand["context"],
            "brand_keywords": json.loads(brand.get("keywords", "[]")) if brand.get("keywords") else [],
        })
    db_check.close()

    if not valid_posts:
        return jsonify({"error": "No eligible posts to generate for"}), 400

    def task(_task_id=None):
        proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
        api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
        db2 = Database(DB_PATH)
        db2.connect()
        db2.initialize()
        cg = CommentGenerator(
            ClaudeClient(api_key), db2,
            reddit_base=proxy.rstrip("/") if proxy else None,
        )
        results = []
        try:
            for i, vp in enumerate(valid_posts):
                pid = vp["pid"]
                if _task_id:
                    try:
                        progress_db = Database(DB_PATH)
                        progress_db.connect()
                        progress_db.update_task_progress(_task_id, {
                            "current": i + 1,
                            "total": len(valid_posts),
                            "generated": len([r for r in results if r.get("generated")]),
                            "skipped": len([r for r in results if r.get("skipped")]),
                            "errors": len([r for r in results if r.get("error")]),
                        })
                        progress_db.close()
                    except Exception:
                        pass

                try:
                    res = _generate_hq_for_search_post(
                        cg, db2, pid, vp["post"], vp["brand"],
                        vp["brand_name"], vp["brand_context"], vp["brand_keywords"],
                        relevance_threshold=threshold,
                        ai_crawl=ai_crawl,
                    )
                    res["pid"] = pid
                    results.append(res)
                except Exception as e:
                    db2.update_search_post_status(pid, "saved")
                    results.append({"pid": pid, "error": str(e)})
                    print(f"[HQ-BATCH ERROR] post {pid}: {e}", flush=True)
            return {"total": len(valid_posts), "results": results}
        finally:
            db2.close()

    tid = start_task("generate-search-hq-batch", task, pass_task_id=True)
    return jsonify({"task_id": tid, "post_count": len(valid_posts)})


@app.route("/api/search/posts/generate-batch", methods=["POST"])
def api_generate_search_comments_batch():
    """Generate comments for multiple search posts sequentially in one background task.
    Uses the consolidated CommentGenerator."""
    from generators.comment_gen import CommentGenerator
    from generators.base import ClaudeClient
    data = request.json or {}
    post_ids = data.get("post_ids", [])
    num_comments = data.get("num_comments", 2)
    # Pre-consolidation parity for Live Search batch — default ai_crawl off.
    ai_crawl = bool(data.get("ai_crawl", False))
    # Server-side dedup/exclude so the client doesn't pull the whole posts+comments
    # tables to filter (that froze "Save & Generate All" on large datasets).
    dedup = bool(data.get("dedup", False))
    exclude_subs = {(s or "").strip().lower() for s in (data.get("exclude_subs") or [])}

    if not post_ids:
        return jsonify({"error": "No post_ids provided"}), 400

    # Validate all posts upfront
    db_check = get_db()
    valid_posts = []
    for pid in post_ids:
        post = db_check.get_search_post(pid)
        if not post:
            continue
        if post.get("status") in ("generating", "irrelevant"):
            continue
        brand = db_check.get_brand(post["brand_id"]) if post.get("brand_id") else None
        if not brand:
            continue
        if dedup:
            if (post.get("subreddit") or "").strip().lower() in exclude_subs:
                continue
            if db_check.search_post_has_comments_for_brand(pid, post["brand_id"], hq=False):
                continue
        # Stash the full brand dict so the worker can extract focus / any
        # other future field via cg._extract_brand_focus(vp["brand"]).
        valid_posts.append({
            "pid": pid, "post": post, "brand": brand,
            "brand_name": brand["name"],
            "brand_context": brand["context"],
            "brand_keywords": json.loads(brand.get("keywords", "[]")) if brand.get("keywords") else []
        })
    db_check.close()

    if not valid_posts:
        return jsonify({"error": "No eligible posts to generate for"}), 400

    def task(_task_id=None):
        proxy = REDDIT_PROXY_URL or os.environ.get("REDDIT_PROXY_URL", "")
        api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
        db2 = Database(DB_PATH)
        db2.connect()
        db2.initialize()
        cg = CommentGenerator(
            ClaudeClient(api_key), db2,
            reddit_base=proxy.rstrip("/") if proxy else None,
        )
        results = []
        try:
            for i, vp in enumerate(valid_posts):
                pid = vp["pid"]
                post = vp["post"]
                brand_name = vp["brand_name"]
                brand_context = vp["brand_context"]
                brand_keywords = vp["brand_keywords"]

                if _task_id:
                    try:
                        progress_db = Database(DB_PATH)
                        progress_db.connect()
                        progress_db.update_task_progress(_task_id, {
                            "current": i + 1,
                            "total": len(valid_posts),
                            "generated": len([r for r in results if r.get("generated")]),
                            "skipped": len([r for r in results if r.get("skipped")]),
                            "errors": len([r for r in results if r.get("error")]),
                        })
                        progress_db.close()
                    except Exception:
                        pass

                try:
                    db2.update_search_post_status(pid, "generating")

                    comments, post_body, is_archived = cg.fetch_comments(post["reddit_url"], limit=20)
                    if is_archived:
                        db2.update_search_post_status(pid, "saved")
                        results.append({"pid": pid, "skipped": True, "reason": "Post is archived"})
                        continue
                    # No comment-count gate — RSS counts are unreliable, so
                    # 0 comments is allowed (gen proceeds regardless).

                    comment_stats = cg._compute_comment_stats(comments)

                    relevance = cg.check_relevance(
                        post["title"], post_body, post["subreddit"],
                        comments, brand_name, brand_context, brand_keywords,
                    )
                    rel_score = relevance.get("score", 0)
                    threshold = data.get("relevance_threshold", 6)
                    if rel_score < threshold:
                        db2.update_search_post_status(pid, "irrelevant")
                        results.append({
                            "pid": pid, "skipped": True,
                            "relevance_score": rel_score,
                            "comments_fetched": len(comments),
                            "comments_source": getattr(cg, "last_fetch", {}).get("source"),
                            "post_title": (post.get("title") or "")[:80],
                            "subreddit": post.get("subreddit"),
                            "relevance_breakdown": {
                                k: relevance.get(k) for k in
                                ("topic_match", "problem_fit", "natural_fit",
                                 "conversation_opening", "disqualified",
                                 "disqualify_reason", "summary")
                                if k in relevance
                            },
                            "reason": f"Low relevance ({rel_score}) — "
                                      f"topic={relevance.get('topic_match')}/3 "
                                      f"problem={relevance.get('problem_fit')}/3 "
                                      f"on '{(post.get('title') or '')[:50]}' in r/{post.get('subreddit')}",
                        })
                        continue

                    tone_analysis = cg.analyze_tone(
                        post["title"], post_body, post["subreddit"],
                        comments, comment_stats,
                    )

                    reply_targets = {}
                    if num_comments >= 3:
                        reply_target = cg._select_reply_target(
                            comments, post["title"], brand_name, relevance,
                        )
                        if reply_target:
                            reply_targets[2] = reply_target

                    mention_flags = [True] * num_comments
                    brand_focus = cg._extract_brand_focus(vp.get("brand"))
                    generation = cg.generate_comments(
                        post_title=post["title"],
                        post_body=post_body,
                        subreddit=post["subreddit"],
                        comments=comments,
                        brand_name=brand_name,
                        brand_context=brand_context,
                        num_comments=num_comments,
                        tone_analysis=tone_analysis,
                        comment_stats=comment_stats,
                        relevance=relevance,
                        reply_targets=reply_targets,
                        mention_brand_flags=mention_flags,
                        all_brand_names=[brand_name],
                        ai_crawl=ai_crawl,
                        brand_focus=brand_focus,
                    )

                    generated = generation.get("generated_comments", []) or []
                    if not generated:
                        db2.update_search_post_status(pid, "saved")
                        results.append({"pid": pid, "error": "Generation failed"})
                        continue

                    # Brand-mention enforcement retry
                    missing = [j + 1 for j, c in enumerate(generated)
                               if brand_name.lower() not in c.lower()]
                    if missing:
                        feedback = f"Comment(s) {missing} don't mention '{brand_name}'. Naturally weave in a mention."
                        retry_gen = cg.generate_comments(
                            post_title=post["title"],
                            post_body=post_body,
                            subreddit=post["subreddit"],
                            comments=comments,
                            brand_name=brand_name,
                            brand_context=brand_context,
                            num_comments=num_comments,
                            tone_analysis=tone_analysis,
                            comment_stats=comment_stats,
                            retry_feedback=feedback,
                            relevance=relevance,
                            reply_targets=reply_targets,
                            mention_brand_flags=mention_flags,
                            all_brand_names=[brand_name],
                            ai_crawl=ai_crawl,
                            brand_focus=brand_focus,
                        )
                        retry_comments = retry_gen.get("generated_comments", []) or []
                        if retry_comments:
                            retry_missing = [j for j, c in enumerate(retry_comments)
                                             if brand_name.lower() not in c.lower()]
                            if len(retry_missing) < len(missing):
                                generated = retry_comments
                                generation = retry_gen

                    personas_meta = generation.get("_personas") or []
                    stored = []
                    for idx, body in enumerate(generated):
                        is_reply = 1 if idx == 2 and reply_targets.get(2) else 0
                        reply_url = None
                        if is_reply and reply_targets.get(2):
                            rt = reply_targets[2]
                            reply_url = (f"https://www.reddit.com{rt.get('permalink', '')}"
                                         if rt.get('permalink') else None)
                        mentions = 1 if brand_name.lower() in body.lower() else 0
                        cid = db2.add_search_comment(
                            search_post_id=pid, body=body, brand_id=post.get("brand_id"),
                            persona_id=personas_meta[idx] if idx < len(personas_meta) else None,
                            is_reply=is_reply, reply_to_url=reply_url,
                            mentions_brand=mentions, relevance_score=rel_score,
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

    tid = start_task("generate-search-comments-batch", task, pass_task_id=True)
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


@app.route("/api/search/comments/<int:cid>/reassign", methods=["POST"])
def api_reassign_search_comment(cid):
    """Change account_id on an already-assigned/informed search comment without
    resetting status, assigned_at, informed_at, or prev_status."""
    db = get_db()
    try:
        data = request.json or {}
        new_account = (data.get("account_id") or "").strip()
        if not new_account:
            return jsonify({"error": "account_id is required"}), 400
        db.reassign_search_comment(cid, new_account)
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
        # If this comment is the parent of an HQ thread, back-fill its
        # children's reply_to_url so the user can deploy each reply
        # straight to the parent on Reddit without manual URL copy/paste.
        try:
            db.conn.execute(
                "UPDATE search_comments SET reply_to_url = ? "
                "WHERE parent_comment_id = ? "
                "  AND (reply_to_url IS NULL OR reply_to_url = '')",
                (url, cid)
            )
            db.conn.commit()
        except Exception as e:
            print(f"[HQ deploy backfill] cid={cid}: {e}", flush=True)
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


@app.route("/api/search/comments/bulk-mark-paid", methods=["POST"])
def api_bulk_mark_paid_search_comments():
    """Mark multiple search comments as paid in one query.

    Body: { comment_ids: [int, ...] }

    Only comments currently in 'deployed' status are updated — same gate
    as `mark_search_comment_paid` for a single comment. Filtering by
    other criteria (brand / subreddit / post / date) is the caller's job:
    the Live Search Comments tab applies its current filters client-side
    and passes only the matching IDs here.
    """
    data = request.json or {}
    ids = data.get("comment_ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "comment_ids must be a non-empty list"}), 400
    db = get_db()
    try:
        # Defensive: cast to int + dedupe
        ids = list({int(i) for i in ids})
        placeholders = ",".join("?" * len(ids))
        cur = db.conn.execute(
            f"""UPDATE search_comments
                SET status = 'paid', paid_at = datetime('now'), prev_status = status
                WHERE id IN ({placeholders}) AND status = 'deployed'""",
            ids
        )
        updated = cur.rowcount
        db.conn.commit()
        return jsonify({"updated": updated, "requested": len(ids)})
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
        restored = db.unremove_search_comment(cid)
        return jsonify({"ok": True, "status": restored or "deployed"})
    finally:
        db.close()

@app.route("/api/search/comments/<int:cid>/undo", methods=["POST"])
def api_undo_search_comment(cid):
    db = get_db()
    try:
        prev = db.undo_search_comment_status(cid)
        if prev is None:
            return jsonify({"error": "No previous status to undo"}), 400
        return jsonify({"ok": True, "new_status": prev})
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
        # search_subreddits accepts list or comma-separated string from the UI
        ss_raw = data.get("search_subreddits")
        if isinstance(ss_raw, list):
            ss_list = [str(s).strip() for s in ss_raw if str(s).strip()]
        elif isinstance(ss_raw, str):
            ss_list = [s.strip() for s in ss_raw.replace("\n", ",").split(",") if s.strip()]
        else:
            ss_list = []
        search_subreddits = json.dumps(ss_list) if ss_list else None
        cur = db.conn.execute(
            "INSERT INTO brands (subreddit_id, name, domain_url, context, keywords, search_subreddits) VALUES (NULL, ?, ?, ?, ?, ?)",
            (name, domain_url, context, keywords, search_subreddits)
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


@app.route("/api/posts/<int:pid>/subreddit", methods=["PUT"])
def api_set_post_subreddit(pid):
    """Reassign a live-subs post to a different subreddit.
    Unconditional — works at any lifecycle stage (draft / complete /
    deployed / paid). The Reddit post itself doesn't move; this is
    an internal organization knob so the admin can re-classify which
    sub a post counts against on the dashboard / generator.

    Accepts either `subreddit_id` (an existing row in `subreddits`)
    or `subreddit_name` (a sub name — used when the picker comes
    from the brand's saved list, which carries names not ids).
    For `subreddit_name`, the existing `ensure_live_subreddit`
    helper finds or auto-provisions the row.
    """
    db = get_db()
    try:
        data = request.json or {}
        sub_id = data.get("subreddit_id")
        sub_name = (data.get("subreddit_name") or "").strip()
        try:
            sub_id_int = int(sub_id) if sub_id not in (None, '', 0) else None
        except (TypeError, ValueError):
            sub_id_int = None
        if not sub_id_int and not sub_name:
            return jsonify({"error": "subreddit_id or subreddit_name required"}), 400
        if not sub_id_int:
            # Resolve by name — auto-create as a live row if missing.
            row = db.ensure_live_subreddit(sub_name)
            if not row:
                return jsonify({"error": "could not resolve subreddit_name"}), 400
            sub_id_int = int(row["id"])
        else:
            # Verify the target id exists.
            row = db.conn.execute(
                "SELECT id FROM subreddits WHERE id = ?", (sub_id_int,)
            ).fetchone()
            if not row:
                return jsonify({"error": "subreddit_id not found"}), 404
        db.conn.execute(
            "UPDATE posts SET subreddit_id = ? WHERE id = ?",
            (sub_id_int, pid)
        )
        db.conn.commit()
        return jsonify({"ok": True, "subreddit_id": sub_id_int})
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

# ===========================================================================
# Client Reporting Module — admin CRUD + client-facing portal
# ===========================================================================
#
# Two surfaces live below:
#
# 1. Admin endpoints (gated by the existing Google OAuth in
#    `require_login`) — CRUD for clients, and per-row + bulk
#    actions to push deployed/paid comments into the new 'report'
#    state.
#
# 2. Client portal (separate password-based auth, lives under
#    /portal/*) — agency clients log in with one of their associated
#    emails and see only their own monthly report deliverables.
#    Reuses the existing `_check_live_batch` and the same CSV column
#    shape the admin sees in `lsExportCsv`.
#
# Admin "view as client": admins with a valid OAuth session can view
# any client's portal by appending `?as=<client_id>` to a /portal/*
# URL. A sticky banner identifies the admin view; no client password
# is required. Row-level access control still applies — the admin
# sees only that one client's brands.

from werkzeug.security import generate_password_hash, check_password_hash
from flask import render_template, abort, make_response
import io as _io
import csv as _csv
import re


def _admin_email():
    """Current admin's email from the OAuth session (or empty)."""
    return (session.get('user_email') or '').lower()


def _admin_authed():
    """True if the request has a valid admin OAuth session OR auth
    is disabled (dev mode)."""
    if not _auth_enabled:
        return True
    return bool(session.get('user_email'))


# ----- Admin endpoints: client CRUD -----------------------------------------

@app.route('/api/clients', methods=['GET'])
def api_list_clients():
    db = get_db()
    try:
        return jsonify(db.list_clients())
    finally:
        db.close()


@app.route('/api/clients', methods=['POST'])
def api_create_client():
    """Create a client.

    `password` is optional. When omitted, we generate a random
    placeholder password the client never sees and instead return an
    `invite_url` the admin can hand over (or auto-email if SMTP is
    set + `email_invite: true` is in the body). The client uses that
    invite link — same flow as a password reset — to set their own
    password on first sign-in.
    """
    import secrets as _secrets
    import hashlib as _hashlib
    from datetime import datetime, timedelta

    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    password = (data.get('password') or '').strip()
    emails = data.get('emails') or []
    brand_ids = data.get('brand_ids') or []
    monthly_target = data.get('monthly_target')
    notes = (data.get('notes') or '').strip() or None
    email_invite = bool(data.get('email_invite'))
    if not name or not emails:
        return jsonify({"error": "name and at least one email are required"}), 400
    # If password is blank, we still set a random one so the row is
    # always usable — but it's overwritten by the invite-link flow
    # before the client ever logs in.
    auto_password = False
    if not password:
        password = _secrets.token_urlsafe(32)
        auto_password = True
    db = get_db()
    try:
        # Reject if any email is already on another client.
        for e in emails:
            existing = db.conn.execute(
                "SELECT client_id FROM client_emails WHERE LOWER(email) = LOWER(?)",
                (e,)
            ).fetchone()
            if existing:
                return jsonify({"error": f"email already in use: {e}"}), 409
        cid = db.create_client(
            name=name,
            password_hash=generate_password_hash(password),
            monthly_target=monthly_target,
            notes=notes,
        )
        db.set_client_emails(cid, emails)
        db.set_client_brands(cid, brand_ids)
        # Always generate an invite link when no explicit password was
        # provided — that's how the client will set their own. Also
        # provide it when password WAS set, so admins have a backup
        # delivery method.
        ttl_days = int(os.environ.get('ADMIN_INVITE_TTL_DAYS', '7'))
        raw = _secrets.token_urlsafe(40)
        token_hash = _hashlib.sha256(raw.encode('utf-8')).hexdigest()
        expires_at = (datetime.utcnow() + timedelta(days=ttl_days)).strftime('%Y-%m-%d %H:%M:%S')
        db.create_password_reset_token(
            client_id=cid, token_hash=token_hash,
            expires_at=expires_at, requested_email='admin-generated',
            requested_ip=(request.headers.get('X-Forwarded-For', '') or request.remote_addr or ''),
        )
        invite_url = request.url_root.rstrip('/') + f"/portal/reset-password/{raw}"
        emailed = False
        if email_invite and emails:
            primary_email = emails[0].strip().lower()
            body = (
                f"Hi,\n\n"
                f"You've been invited to the {name} client portal.\n"
                f"Click the link below to set your password (valid for {ttl_days} days):\n\n"
                f"{invite_url}\n\n"
                f"If you didn't expect this email, ignore it.\n"
            )
            emailed = _send_email(
                to_addr=primary_email,
                subject=f"{name} portal — set your password",
                body_text=body,
            )
        return jsonify({
            "id": cid, "ok": True,
            "invite_url": invite_url,
            "invite_expires_at": expires_at,
            "auto_password": auto_password,
            "emailed": emailed,
        })
    finally:
        db.close()


@app.route('/api/clients/<int:cid>', methods=['GET'])
def api_get_client(cid):
    db = get_db()
    try:
        c = db.get_client(cid)
        if not c:
            return jsonify({"error": "not_found"}), 404
        # Don't ever leak the password hash.
        c.pop('password_hash', None)
        return jsonify(c)
    finally:
        db.close()


@app.route('/api/clients/<int:cid>', methods=['PUT'])
def api_update_client(cid):
    data = request.get_json() or {}
    db = get_db()
    try:
        existing = db.get_client(cid)
        if not existing:
            return jsonify({"error": "not_found"}), 404
        kwargs = {}
        for k in ('name', 'monthly_target', 'notes'):
            if k in data:
                kwargs[k] = data[k]
        # Password change is explicit + requires the new value
        new_pw = data.get('password')
        if new_pw:
            kwargs['password_hash'] = generate_password_hash(new_pw)
        if kwargs:
            db.update_client(cid, **kwargs)
        if 'emails' in data:
            # Same uniqueness check
            for e in data['emails'] or []:
                row = db.conn.execute(
                    "SELECT client_id FROM client_emails WHERE LOWER(email) = LOWER(?) AND client_id != ?",
                    (e, cid)
                ).fetchone()
                if row:
                    return jsonify({"error": f"email already in use: {e}"}), 409
            db.set_client_emails(cid, data['emails'])
        if 'brand_ids' in data:
            db.set_client_brands(cid, data['brand_ids'])
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route('/api/clients/<int:cid>', methods=['DELETE'])
def api_delete_client(cid):
    db = get_db()
    try:
        db.delete_client(cid)
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route('/api/clients/<int:cid>/invite-link', methods=['POST'])
def api_client_invite_link(cid):
    """Generate a single-use password-reset link for a client.

    Use cases:
      - First-time onboarding: admin creates the client without a
        password and sends them this link to set their own.
      - Password forgotten + SMTP not configured: admin generates a
        fresh link and hand-delivers it.
      - Re-issue after a lost / leaked password.

    The returned link uses the same flow as `/portal/forgot-password`
    but with a longer TTL (7 days by default — configurable via
    ADMIN_INVITE_TTL_DAYS). Admin can also choose to email it
    automatically if SMTP is wired up.
    """
    import secrets as _secrets
    import hashlib as _hashlib
    from datetime import datetime, timedelta

    data = request.get_json() or {}
    send_email = bool(data.get('send_email'))
    ttl_days = int(os.environ.get('ADMIN_INVITE_TTL_DAYS', '7'))
    db = get_db()
    try:
        client = db.get_client(cid)
        if not client:
            return jsonify({"error": "not_found"}), 404
        raw = _secrets.token_urlsafe(40)
        token_hash = _hashlib.sha256(raw.encode('utf-8')).hexdigest()
        expires_at = (datetime.utcnow() + timedelta(days=ttl_days)).strftime('%Y-%m-%d %H:%M:%S')
        db.create_password_reset_token(
            client_id=cid,
            token_hash=token_hash,
            expires_at=expires_at,
            requested_email='admin-generated',
            requested_ip=(request.headers.get('X-Forwarded-For', '') or request.remote_addr or ''),
        )
        link = request.url_root.rstrip('/') + f"/portal/reset-password/{raw}"
        # Optional: email the link to the client's primary email.
        emailed = False
        if send_email and client.get('primary_email'):
            body = (
                f"Hi,\n\n"
                f"You've been invited to the {client['name']} client portal.\n"
                f"Click the link below to set your password (valid for {ttl_days} days):\n\n"
                f"{link}\n\n"
                f"If you didn't expect this email, ignore it.\n"
            )
            emailed = _send_email(
                to_addr=client['primary_email'],
                subject=f"{client['name']} portal — set your password",
                body_text=body,
            )
        return jsonify({
            "url": link,
            "expires_at": expires_at,
            "ttl_days": ttl_days,
            "emailed": emailed,
            "primary_email": client.get('primary_email'),
        })
    finally:
        db.close()


# ----- Admin endpoints: report lifecycle -----------------------------------

_REPORT_MONTH_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')


def _valid_report_month(m):
    return bool(m and _REPORT_MONTH_RE.match(m))


def _fetch_report_items(db, ids):
    """Resolve a list of {id, source} to the full payload
    `_check_live_batch` expects: id, source, reddit_comment_url,
    status, account_id, brand_name, subreddit. Skips rows missing
    a URL — there's nothing to fetch in that case.
    """
    out = []
    for entry in ids:
        try:
            cid = int(entry.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        src = entry.get("source") or "comment"
        if src == "comment":
            row = db.conn.execute(
                """SELECT c.id, c.reddit_comment_url, c.status, c.account_id,
                          b.name AS brand_name, s.name AS subreddit
                     FROM comments c
                     JOIN posts p ON c.post_id = p.id
                LEFT JOIN brands b ON c.brand_id = b.id
                LEFT JOIN subreddits s ON p.subreddit_id = s.id
                    WHERE c.id = ?""",
                (cid,)
            ).fetchone()
        else:
            row = db.conn.execute(
                """SELECT sc.id, sc.reddit_comment_url, sc.status, sc.account_id,
                          b.name AS brand_name, sp.subreddit AS subreddit
                     FROM search_comments sc
                     JOIN search_posts sp ON sc.search_post_id = sp.id
                LEFT JOIN brands b ON sc.brand_id = b.id
                    WHERE sc.id = ?""",
                (cid,)
            ).fetchone()
        if not row or not row["reddit_comment_url"]:
            continue
        out.append({
            "id": row["id"], "source": src,
            "reddit_comment_url": row["reddit_comment_url"],
            "status": row["status"], "account_id": row["account_id"],
            "brand_name": row["brand_name"], "subreddit": row["subreddit"],
        })
    return out


def _spawn_report_engagement_fetch(items):
    """Fire-and-forget background fetch of Reddit engagement (upvotes,
    num_replies) for newly-reported comments. Reuses `_check_live_batch`
    so we also benefit from its dead-comment detection — a comment that
    was paid yesterday and is gone today will land in 'removed' status
    instead of staying 'live' on the dashboard with stale numbers.

    Empty `items` is a no-op. We start the task lazily so the parent
    request returns immediately.
    """
    if not items:
        return None

    def task():
        bg = Database(DB_PATH)
        bg.connect(); bg.initialize()
        try:
            return _check_live_batch(items, bg, log_prefix="REPORT-AUTO-STATS")
        finally:
            try: bg.close()
            except Exception: pass

    try:
        return start_task("report-auto-stats", task)
    except Exception as e:
        # Best-effort. The user can always trigger a manual refresh
        # from the portal if this fails for any reason.
        print(f"[REPORT-AUTO-STATS] spawn failed: {e}", flush=True)
        return None


def _report_live_gate(db, items, task_id=None):
    """NON-MUTATING liveness gate for the report flow. For each {id, source}, fetch the
    comment's Reddit metadata and classify it — but NEVER change any row's status (unlike
    the regular live-checker, which moves dead ones to 'removed'/'replace'). Only comments
    confirmed LIVE may be reported; everything else (removed / missing / no URL / fetch
    error) is left exactly as-is.

    Returns {"live": [{id, source}], "not_live": [{id, source, liveness}]}.
    Writes progress to the task when `task_id` is given.
    """
    from bulk_deploy import fetch_reddit_comment_meta, classify_liveness
    live, not_live = [], []
    total = len(items or [])
    done = 0
    for it in (items or []):
        try:
            cid = int(it.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        src = it.get("source") or "comment"
        table = "comments" if src == "comment" else "search_comments"
        row = db.conn.execute(
            f"SELECT reddit_comment_url FROM {table} WHERE id = ?", (cid,)).fetchone()
        url = ((row["reddit_comment_url"] if row else "") or "").strip()
        liveness = "missing"
        if url:
            try:
                meta = fetch_reddit_comment_meta(url, reddit_get=_reddit_get_json)
                liveness = classify_liveness(meta)
            except Exception:
                liveness = "missing"
        if liveness == "live":
            live.append({"id": cid, "source": src})
        else:
            not_live.append({"id": cid, "source": src, "liveness": liveness})
        done += 1
        if task_id:
            try:
                db.update_task_progress(task_id, {
                    "checked": done, "total": total,
                    "live": len(live), "skipped": len(not_live)})
            except Exception:
                pass
    return {"live": live, "not_live": not_live}


@app.route('/api/comments/<int:cid>/to-report', methods=['POST'])
def api_comment_to_report(cid):
    data = request.get_json() or {}
    month = (data.get('report_month') or '').strip()
    source = (data.get('source') or 'comment').strip()
    # outcome: 'report' (deployed/paid → report) or 'replaced' (replace → replaced).
    # BOTH are live-gated — only a comment that's actually live on Reddit is accepted
    # (for 'replaced' this confirms the replacement at its link is up).
    outcome = (data.get('outcome') or 'report').strip()
    brand_id = data.get('brand_id')
    try:
        brand_id = int(brand_id) if brand_id not in (None, '', 0) else None
    except (TypeError, ValueError):
        brand_id = None
    if not _valid_report_month(month):
        return jsonify({"error": "report_month must be YYYY-MM"}), 400
    if source not in ('comment', 'search_comment'):
        return jsonify({"error": "source must be 'comment' or 'search_comment'"}), 400
    if outcome not in ('report', 'replaced'):
        return jsonify({"error": "outcome must be 'report' or 'replaced'"}), 400
    # Reported and Replaced are the same flow on live deployed/paid comments — only the
    # final status label differs. (A 'replaced' comment is a fresh replacement comment
    # the operator deployed, then categorizes as 'replaced' rather than 'report'.)
    target_status = 'replaced' if outcome == 'replaced' else 'report'
    allowed_from = ('deployed', 'paid')
    db = get_db()
    try:
        # Live gate (both outcomes): only accept a comment that's actually live on Reddit.
        # A non-live one is left at its current status (never moved to removed). For
        # 'replaced' this verifies the replacement at the comment's link is live.
        gate = _report_live_gate(db, [{"id": cid, "source": source}])
        if not gate["live"]:
            nl = gate["not_live"][0] if gate["not_live"] else {"liveness": "missing"}
            return jsonify({"ok": False, "skipped": True, "liveness": nl.get("liveness"),
                            "message": "left unchanged — not live on Reddit"}), 200
        result = db.move_comment_to_report(
            cid, source, month,
            actor_email=_admin_email(), brand_id=brand_id,
            target_status=target_status, allowed_from=allowed_from,
        )
        if result is db.BRAND_REQUIRED:
            # Tell the UI which brands to offer. Empty list →
            # client falls back to /api/brands/all.
            candidates = db.report_brand_candidates(cid, source)
            return jsonify({"error": "brand_required",
                            "candidates": candidates,
                            "message": "We couldn't infer this comment's brand. Pick one to attribute it to a client."}), 422
        if result is None:
            return jsonify({"error": "row not found or not in deployed/paid status"}), 422
        # Kick off a background fetch so the client dashboard picks
        # up upvotes/replies without the admin needing to click
        # anything else. Fire-and-forget — the report move is already
        # committed.
        items = _fetch_report_items(db, [{"id": cid, "source": source}])
        _spawn_report_engagement_fetch(items)
        return jsonify({"ok": True, "prev_status": result,
                        "report_month": month, "brand_id": brand_id})
    finally:
        db.close()


@app.route('/api/posts/<int:pid>/to-report', methods=['POST'])
def api_post_to_report(pid):
    """Flip a Live Subs post + its comments into status='report'.

    Body:
      {
        "report_month": "YYYY-MM",
        "brand_id": int | null,          # override for unbranded comments
        "comment_ids": [int, ...] | null # optional explicit subset.
                                          # null = all deployed/paid comments
                                          # under the post (plus HQ root).
      }

    Returns {ok, comments_updated, brand_required, post_flipped} or
    422 {error: 'report_month must be YYYY-MM'}.
    """
    data = request.get_json() or {}
    month = (data.get('report_month') or '').strip()
    if not _valid_report_month(month):
        return jsonify({"error": "report_month must be YYYY-MM"}), 400
    try:
        brand_id = int(data['brand_id']) if data.get('brand_id') not in (None, '', 0) else None
    except (TypeError, ValueError):
        brand_id = None
    comment_ids_raw = data.get('comment_ids')
    comment_ids = None
    if isinstance(comment_ids_raw, list):
        comment_ids = set()
        for c in comment_ids_raw:
            try:
                comment_ids.add(int(c))
            except (TypeError, ValueError):
                pass
    db = get_db()
    try:
        post = db.conn.execute("SELECT id FROM posts WHERE id = ?", (pid,)).fetchone()
        if not post:
            return jsonify({"error": "post_not_found"}), 404
        result = db.move_post_to_report(
            pid, report_month=month,
            actor_email=_admin_email(),
            brand_id_override=brand_id,
            comment_ids=comment_ids,
        )
        # Fire-and-forget engagement fetch on the comments we flipped
        # (mirrors the existing comment-level auto-stats hook).
        try:
            ids = [{"id": c["id"], "source": "comment"}
                   for c in db.conn.execute(
                       """SELECT id FROM comments
                           WHERE post_id = ? AND status = 'report'
                             AND report_month = ?""",
                       (pid, month)
                   ).fetchall()]
            items = _fetch_report_items(db, ids)
            _spawn_report_engagement_fetch(items)
        except Exception as e:
            print(f"[posts/to-report] auto-stats spawn failed: {e}", flush=True)
        return jsonify({"ok": True, **result})
    finally:
        db.close()


@app.route('/api/posts/<int:pid>/undo-report', methods=['POST'])
def api_post_undo_report(pid):
    """Revert a 'report' post + all of its reported comments back
    to their previous status. Idempotent."""
    db = get_db()
    try:
        result = db.undo_post_report(pid, actor_email=_admin_email())
        if result.get("post_restored_to") is None:
            return jsonify({"error": "post_not_in_report_state"}), 422
        return jsonify({"ok": True, **result})
    finally:
        db.close()


@app.route('/api/posts/bulk-to-report-filtered', methods=['POST'])
def api_posts_bulk_to_report_filtered():
    """Iterate every Live Subs post matching the given filter set
    and flip each to status='report' (along with its comments).

    Body:
      {
        report_month: "YYYY-MM",
        brand_id?: int,
        subreddit_id?: int,
        status?: "published" | "paid" | null,  # default both
        intent?: "commercial" | "comparison" | "informational",
      }
    """
    data = request.get_json() or {}
    month = (data.get('report_month') or '').strip()
    if not _valid_report_month(month):
        return jsonify({"error": "report_month must be YYYY-MM"}), 400
    try:
        brand_id = int(data['brand_id']) if data.get('brand_id') not in (None, '', 0) else None
    except (TypeError, ValueError):
        brand_id = None
    try:
        sub_id = int(data['subreddit_id']) if data.get('subreddit_id') not in (None, '', 0) else None
    except (TypeError, ValueError):
        sub_id = None
    status_filter = data.get('status') or None
    intent_filter = data.get('intent') or None
    if status_filter not in (None, 'published', 'paid'):
        return jsonify({"error": "status must be 'published' or 'paid' or omitted"}), 400

    db = get_db()
    try:
        # Build a candidate-post query that mirrors the Live Subs
        # Posts tab's filters. status default = published OR paid
        # (the lifecycle states eligible for reporting).
        where = ["p.is_live = 1"]
        params = []
        if status_filter:
            where.append("p.status = ?"); params.append(status_filter)
        else:
            where.append("p.status IN ('published', 'paid')")
        if brand_id:
            where.append(
                "(p.brand_id = ? OR p.id IN (SELECT post_id FROM post_brands WHERE brand_id = ?))"
            )
            params += [brand_id, brand_id]
        if sub_id:
            where.append("p.subreddit_id = ?"); params.append(sub_id)
        if intent_filter:
            where.append("p.intent = ?"); params.append(intent_filter)
        rows = db.conn.execute(
            f"SELECT id FROM posts p WHERE {' AND '.join(where)}",
            params
        ).fetchall()
        post_ids = [r["id"] for r in rows]
        result = db.bulk_move_posts_to_report(
            post_ids=post_ids,
            report_month=month,
            actor_email=_admin_email(),
            brand_id_override=brand_id,
        )
        # Auto-stats for everything just flipped.
        try:
            cmt_ids = db.conn.execute(
                f"""SELECT id FROM comments
                     WHERE post_id IN ({','.join('?' * len(post_ids))})
                       AND status = 'report' AND report_month = ?""",
                post_ids + [month]
            ).fetchall() if post_ids else []
            items = _fetch_report_items(db, [{"id": r["id"], "source": "comment"} for r in cmt_ids])
            _spawn_report_engagement_fetch(items)
        except Exception as e:
            print(f"[posts/bulk-to-report-filtered] auto-stats spawn failed: {e}", flush=True)
        result["candidates_considered"] = len(post_ids)
        return jsonify(result)
    finally:
        db.close()


@app.route('/api/comments/<int:cid>/undo-report', methods=['POST'])
def api_comment_undo_report(cid):
    source = (request.get_json() or {}).get('source', 'comment')
    if source not in ('comment', 'search_comment'):
        return jsonify({"error": "bad source"}), 400
    db = get_db()
    try:
        restored = db.undo_report(cid, source, actor_email=_admin_email())
        if restored is None:
            return jsonify({"error": "row not in report state"}), 422
        return jsonify({"ok": True, "restored_to": restored})
    finally:
        db.close()


@app.route('/api/comments/bulk-to-report', methods=['POST'])
def api_bulk_to_report():
    """Body: {ids: [{id, source}], report_month, brand_id?}

    `brand_id` (optional) is applied as an override to any row in
    the batch whose brand_id is currently NULL. Already-branded
    rows keep their brand untouched.
    """
    data = request.get_json() or {}
    ids = data.get('ids') or []
    month = (data.get('report_month') or '').strip()
    brand_id = data.get('brand_id')
    try:
        brand_id = int(brand_id) if brand_id not in (None, '', 0) else None
    except (TypeError, ValueError):
        brand_id = None
    if not _valid_report_month(month):
        return jsonify({"error": "report_month must be YYYY-MM"}), 400
    db = get_db()
    try:
        # Live gate: only report comments still live on Reddit; non-live ones are left
        # untouched (NOT moved to removed). This path is small (a post's checked comments)
        # so the check runs inline.
        gate = _report_live_gate(db, ids)
        live = gate["live"]
        out = db.bulk_move_to_report(
            ids=live, report_month=month, actor_email=_admin_email(),
            brand_id_override=brand_id,
        )
        out["skipped_not_live"] = len(gate["not_live"])
        out["not_live"] = gate["not_live"]
        # Background engagement fetch for the (live) rows we moved.
        try:
            items = _fetch_report_items(db, live)
            _spawn_report_engagement_fetch(items)
        except Exception as e:
            print(f"[bulk-to-report] auto-stats spawn failed: {e}", flush=True)
        return jsonify(out)
    finally:
        db.close()


@app.route('/api/comments/bulk-to-report-filtered', methods=['POST'])
def api_bulk_to_report_filtered():
    """Move every comment matching the given filter (mirror of
    /api/all-comments/mark-paid-all's shape) into report state.

    Body: {report_month, brand_id?, subreddit_id?,
           account_id?, source?, date?, status?}

    `brand_id` is treated as a filter on candidate rows AND as an
    override for any unbranded rows in the resulting batch. The two
    use cases are the same value in practice — when the admin picks
    a brand, they want exactly those rows.
    """
    data = request.get_json() or {}
    month = (data.get('report_month') or '').strip()
    brand_id = data.get('brand_id')
    try:
        brand_id = int(brand_id) if brand_id not in (None, '', 0) else None
    except (TypeError, ValueError):
        brand_id = None
    if not _valid_report_month(month):
        return jsonify({"error": "report_month must be YYYY-MM"}), 400
    # outcome: 'report' or 'replaced' — SAME flow (deployed/paid → live-gated → target);
    # only the final status label differs. ('replaced' = a fresh replacement comment the
    # operator deployed, categorized as 'replaced' rather than 'report'.)
    outcome = (data.get('outcome') or 'report').strip()
    if outcome not in ('report', 'replaced'):
        return jsonify({"error": "outcome must be 'report' or 'replaced'"}), 400
    target_status = 'replaced' if outcome == 'replaced' else 'report'
    eligible = ('deployed', 'paid')
    status_filter = data.get('status')
    if status_filter not in (None, 'deployed', 'paid', ''):
        return jsonify({"error": "status must be deployed or paid or omitted"}), 400
    # Capture request-scoped values before the background task (no request context inside).
    actor = _admin_email()
    sub_id = int(data['subreddit_id']) if data.get('subreddit_id') else None
    account_id = data.get('account_id') or None
    source = data.get('source') or None
    date = data.get('date') or None

    # The candidate set can be large (up to 10k). For the REPORT outcome each row needs a
    # Reddit liveness fetch, so it runs as a background task; only LIVE comments are moved
    # (non-live left untouched). The REPLACED outcome skips the live gate (its source is
    # already-removed 'replace' comments) but stays a task for symmetry / large batches.
    def task(_task_id=None):
        db = get_db()
        try:
            candidates = db.get_all_comments_global(
                status=status_filter, brand_id=brand_id, subreddit_id=sub_id,
                account_id=account_id, source=source, date=date,
                limit=10000, offset=0, live=None,
            )
            items = (candidates.get('items') if isinstance(candidates, dict) else candidates) or []
            ids = [{"id": r['id'], "source": r.get('source', 'comment')}
                   for r in items if r.get('status') in eligible]
            # Live gate BOTH outcomes — only comments actually live on Reddit are moved;
            # non-live ones are left at their current status (never moved to removed).
            gate = _report_live_gate(db, ids, task_id=_task_id)
            move_ids = gate["live"]; skipped_not_live = len(gate["not_live"])
            out = db.bulk_move_to_report(
                ids=move_ids, report_month=month, actor_email=actor, brand_id_override=brand_id,
                target_status=target_status, allowed_from=eligible)
            out["candidates_considered"] = len(items)
            out["skipped_not_live"] = skipped_not_live
            try:
                _spawn_report_engagement_fetch(_fetch_report_items(db, move_ids))
            except Exception as e:
                print(f"[bulk-to-report-filtered] auto-stats spawn failed: {e}", flush=True)
            return out
        finally:
            db.close()

    tid = start_task("bulk-to-report-filtered", task, pass_task_id=True)
    return jsonify({"task_id": tid})


# ===========================================================================
# Client portal — separate password-based auth + scoped views
# ===========================================================================

def _acting_client_id():
    """Resolve the client this request is acting on:
      1. Admin mode via `?as=<id>` (requires admin OAuth session) —
         the admin can view any client's portal without a password.
      2. Real client login via `session['client_id']`.
    Returns (client_id, is_admin_view) or (None, False) when neither.
    """
    as_param = request.args.get('as')
    if as_param and _admin_authed():
        try:
            cid = int(as_param)
            session['as_client_id'] = cid
            return cid, True
        except (TypeError, ValueError):
            return None, False
    # Sticky admin view from the session
    if 'as_client_id' in session and _admin_authed():
        try:
            return int(session['as_client_id']), True
        except (TypeError, ValueError):
            session.pop('as_client_id', None)
    # Real client
    return session.get('client_id'), False


def client_required(f):
    @wraps(f)
    def wrap(*a, **kw):
        cid, _admin = _acting_client_id()
        if not cid:
            if request.path.startswith('/portal/api'):
                return jsonify({"error": "auth_required"}), 401
            return redirect('/portal/login')
        return f(*a, **kw)
    return wrap


@app.route('/portal')
def portal_root():
    cid, _ = _acting_client_id()
    if not cid:
        return redirect('/portal/login')
    return redirect('/portal/dashboard')


@app.route('/portal/login', methods=['GET'])
def portal_login_form():
    return render_template('portal/login.html', error=None)


@app.route('/portal/login', methods=['POST'])
def portal_login_submit():
    email = (request.form.get('email') or '').strip().lower()
    password = request.form.get('password') or ''
    if not email or not password:
        return render_template('portal/login.html', error='Email and password are required.'), 400
    db = get_db()
    try:
        client = db.get_client_by_email(email)
        if not client or not check_password_hash(client['password_hash'], password):
            return render_template('portal/login.html', error='Invalid email or password.'), 401
        session['client_id'] = client['id']
        db.touch_client_last_login(client['id'])
        return redirect('/portal/dashboard')
    finally:
        db.close()


@app.route('/portal/logout')
def portal_logout():
    session.pop('client_id', None)
    session.pop('as_client_id', None)
    return redirect('/portal/login')


# ----- Password reset (email-driven, self-serve) ---------------------------
#
# Flow:
#   1. Client hits /portal/forgot-password, enters an email.
#   2. If the email matches a client_emails row, we generate a random
#      token (secrets.token_urlsafe), store its SHA-256 hash + expiry
#      in `password_reset_tokens`, and email the raw token to the user
#      as a one-click link. We always show the same "if your email is
#      on file, you'll receive a link" message — never disclose whether
#      a given email exists.
#   3. The client clicks the link → /portal/reset-password/<token>,
#      enters a new password, submits.
#   4. We verify the token (unconsumed, unexpired), update the client's
#      password_hash, and mark the token consumed.
#
# Email delivery: smtplib with SMTP_HOST / SMTP_PORT / SMTP_USER /
# SMTP_PASS / MAIL_FROM env vars. If SMTP_HOST isn't configured, the
# reset link is printed to the console (dev mode) so the admin can
# hand it over manually until SMTP is wired up.

import secrets as _secrets
import hashlib as _hashlib
from datetime import datetime, timedelta


_RESET_TOKEN_TTL_MINUTES = int(os.environ.get('RESET_TOKEN_TTL_MINUTES', '60'))


def _hash_reset_token(raw):
    """SHA-256 the raw token before storing. Even if the DB leaks, the
    raw tokens stay safe (attackers see hashes only).
    """
    return _hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _send_email(to_addr, subject, body_text):
    """Best-effort plain-text email send.

    Reads SMTP config from env. If SMTP_HOST is missing, prints the
    message to stdout instead — useful for dev / staging where no
    mail relay is wired. The reset flow falls back gracefully: the
    user still sees the success page, but the link is in the server
    log instead of their inbox.

    Returns True on a successful network send, False on print-fallback
    or any error (callers don't change behaviour based on this).
    """
    host = os.environ.get('SMTP_HOST', '').strip()
    if not host:
        print(f"[email/dev-fallback] To: {to_addr}\n"
              f"[email/dev-fallback] Subject: {subject}\n"
              f"[email/dev-fallback] ---\n{body_text}\n"
              f"[email/dev-fallback] ---", flush=True)
        return False
    try:
        import smtplib
        from email.message import EmailMessage
        msg = EmailMessage()
        msg['From'] = os.environ.get('MAIL_FROM', 'no-reply@localhost')
        msg['To'] = to_addr
        msg['Subject'] = subject
        msg.set_content(body_text)
        port = int(os.environ.get('SMTP_PORT', '587'))
        user = os.environ.get('SMTP_USER', '').strip()
        pw = os.environ.get('SMTP_PASS', '').strip()
        use_ssl = os.environ.get('SMTP_SSL', 'false').lower() in ('1', 'true', 'yes')
        if use_ssl or port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                if user and pw:
                    s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                try:
                    s.starttls()
                    s.ehlo()
                except Exception:
                    pass  # plain — uncommon but possible for internal relays
                if user and pw:
                    s.login(user, pw)
                s.send_message(msg)
        return True
    except Exception as e:
        print(f"[email] send failed to {to_addr}: {e}", flush=True)
        return False


@app.route('/portal/forgot-password', methods=['GET'])
def portal_forgot_form():
    return render_template('portal/forgot_password.html', sent=False, error=None)


@app.route('/portal/forgot-password', methods=['POST'])
def portal_forgot_submit():
    email = (request.form.get('email') or '').strip().lower()
    # Always render the same "check your inbox" page — never disclose
    # whether the email exists. The actual fork is hidden behind that
    # uniform response.
    db = get_db()
    try:
        client = db.get_client_by_email(email) if email else None
        if client:
            raw = _secrets.token_urlsafe(40)
            token_hash = _hash_reset_token(raw)
            expires_at = (datetime.utcnow() + timedelta(minutes=_RESET_TOKEN_TTL_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')
            db.create_password_reset_token(
                client_id=client['id'],
                token_hash=token_hash,
                expires_at=expires_at,
                requested_email=email,
                requested_ip=(request.headers.get('X-Forwarded-For', '') or request.remote_addr or ''),
            )
            link = request.url_root.rstrip('/') + f"/portal/reset-password/{raw}"
            body = (
                f"Hi,\n\n"
                f"Someone requested a password reset for the {client['name']} client portal.\n"
                f"If this was you, follow the link below to set a new password "
                f"(valid for {_RESET_TOKEN_TTL_MINUTES} minutes):\n\n"
                f"{link}\n\n"
                f"If you didn't request this, ignore this email — your password will not change.\n"
            )
            _send_email(
                to_addr=email,
                subject=f"{client['name']} portal — password reset",
                body_text=body,
            )
    finally:
        db.close()
    return render_template('portal/forgot_password.html', sent=True, error=None)


@app.route('/portal/reset-password/<token>', methods=['GET'])
def portal_reset_form(token):
    db = get_db()
    try:
        row = db.get_password_reset_by_token_hash(_hash_reset_token(token))
        if not row:
            return render_template('portal/reset_password.html', token=None,
                                   error='This reset link is invalid or has expired. Request a new one.',
                                   success=False)
        return render_template('portal/reset_password.html', token=token,
                               error=None, success=False)
    finally:
        db.close()


@app.route('/portal/reset-password/<token>', methods=['POST'])
def portal_reset_submit(token):
    new = request.form.get('new_password') or ''
    confirm = request.form.get('confirm_password') or ''
    if len(new) < 8 or new != confirm:
        db = get_db()
        try:
            valid = bool(db.get_password_reset_by_token_hash(_hash_reset_token(token)))
        finally:
            db.close()
        return render_template('portal/reset_password.html',
                               token=token if valid else None,
                               error='Password must be at least 8 characters and match confirmation.',
                               success=False), 400
    db = get_db()
    try:
        token_hash = _hash_reset_token(token)
        row = db.get_password_reset_by_token_hash(token_hash)
        if not row:
            return render_template('portal/reset_password.html', token=None,
                                   error='This reset link is invalid or has expired. Request a new one.',
                                   success=False), 410
        db.update_client(row['client_id'], password_hash=generate_password_hash(new))
        db.consume_password_reset_token(token_hash)
        # Best-effort cleanup of stale tokens to keep the table tidy
        try: db.cleanup_expired_password_resets()
        except Exception: pass
        return render_template('portal/reset_password.html', token=None,
                               error=None, success=True)
    finally:
        db.close()


@app.route('/portal/exit-admin-view')
def portal_exit_admin_view():
    session.pop('as_client_id', None)
    return redirect('/')


@app.route('/portal/dashboard')
@client_required
def portal_dashboard():
    cid, is_admin = _acting_client_id()
    db = get_db()
    try:
        client = db.get_client(cid)
        if not client:
            return abort(404)
        months = db.get_report_months_for_client(cid)
        # Flat per-(brand, month, status) aggregate for the
        # "By Brand" tab. JSON-serialized into the template so
        # JS can slice/filter without another round-trip.
        brand_rows = db.get_report_aggregate_for_client(cid)
        return render_template(
            'portal/dashboard.html',
            client=client, months=months,
            brand_rows=brand_rows,
            is_admin_view=is_admin,
        )
    finally:
        db.close()


@app.route('/portal/brand/<int:brand_id>')
@client_required
def portal_brand(brand_id):
    """Brand overview page. Lists every month that has at least one
    reported comment under this brand for the active client, as month
    tiles (same shape as the dashboard's By Month grid). Clicking a
    tile drills into /portal/month/<m>?brand=<brand_name> so the
    filter follows the user through.
    """
    cid, is_admin = _acting_client_id()
    db = get_db()
    try:
        client = db.get_client(cid)
        if not client:
            return abort(404)
        # Authorization: the client must actually have this brand on
        # their `client_brands` row. Otherwise admins could in theory
        # construct a URL referring to a brand the client doesn't
        # own — even in admin-view that's a confusing surface.
        client_brand_ids = set(db.client_brand_ids(cid))
        if brand_id not in client_brand_ids:
            return abort(404)
        # Resolve brand name; bail if the brand row doesn't exist.
        brand_row = db.conn.execute(
            "SELECT id, name FROM brands WHERE id = ?", (brand_id,)
        ).fetchone()
        if not brand_row:
            return abort(404)
        # Reuse the existing aggregate query — already scoped to the
        # client's brands — and filter to this one brand. The aggregate
        # returns a flat (brand, month) grid; collapse to per-month.
        # Keep the per-pipeline counts so the template can render
        # Mentions and HQ Mentions separately.
        agg = db.get_report_aggregate_for_client(cid)
        keys = ("mentions_total", "mentions_live", "mentions_removed",
                "hq_total", "hq_live", "hq_removed",
                "total", "live", "removed")
        per_month = {}
        for r in agg:
            if r['brand_id'] != brand_id:
                continue
            m = r['month']
            slot = per_month.setdefault(m, {"month": m, **{k: 0 for k in keys}})
            for k in keys:
                slot[k] += r.get(k) or 0
        months = sorted(per_month.values(), key=lambda r: r["month"], reverse=True)
        return render_template(
            'portal/brand.html',
            client=client, brand=dict(brand_row),
            months=months, is_admin_view=is_admin,
        )
    finally:
        db.close()


@app.route('/portal/month/<month>')
@client_required
def portal_month(month):
    if not _valid_report_month(month):
        return abort(404)
    cid, is_admin = _acting_client_id()
    db = get_db()
    try:
        client = db.get_client(cid)
        if not client:
            return abort(404)
        # Post-grouped view: Live Subs comments are clustered under
        # their parent post; Live Search comments are kept flat
        # (they have no post container). Total/live/removed KPIs are
        # derived from the union below in the template.
        grouped = db.get_posts_for_client_month(cid, month)
        # Keep the flat row list around for KPI math + the search
        # comments section (rendered as before).
        rows = db.get_comments_for_client_month(cid, month)
        return render_template(
            'portal/month.html',
            client=client, month=month, rows=rows,
            posts=grouped["posts"], search_comments=grouped["search_comments"],
            is_admin_view=is_admin,
        )
    finally:
        db.close()


@app.route('/portal/api/find-link')
@client_required
def portal_find_link():
    """Resolve a pasted Reddit post/comment link to its location in THIS
    client's monthly reports. Returns JSON:
      {found:true, month, brand, fid, kind}  — fid = the immutable post/comment
        id the month page highlights; kind = 'comment' | 'hq' | 'mention'.
      {found:false, reason}                  — unrecognized / not reported / not theirs.
    Strictly client-scoped (report_client_id, else the client's brand list) so a
    client can never resolve a link to records they don't own. Reuses the same
    URL parsing + matching as admin Bulk Deploy."""
    from bulk_deploy import classify_reddit_url
    cid, _is_admin = _acting_client_id()
    url = (request.args.get('url') or '').strip()
    if not url:
        return jsonify({"found": False, "reason": "Paste a Reddit post or comment link."})
    parsed = classify_reddit_url(url)
    if not parsed:
        return jsonify({"found": False, "reason": "That doesn't look like a Reddit post or comment link."})
    db = get_db()
    try:
        brand_ids = set(db.client_brand_ids(cid))

        def _owned(row, post_brand_id=None):
            rcid = row.get("report_client_id")
            if rcid is not None:
                return int(rcid) == int(cid)
            bid = row.get("brand_id") or post_brand_id
            return bid in brand_ids if bid is not None else False

        def _brand_name(bid):
            if not bid:
                return ""
            r = db.conn.execute("SELECT name FROM brands WHERE id = ?", (bid,)).fetchone()
            return (r["name"] if r else "") or ""

        # --- Comment link: match in legacy comments, then Live Search comments ---
        if parsed.get("comment_id"):
            comment_id = parsed["comment_id"]
            row = db.find_comment_by_url(url) or db.find_comment_by_reddit_comment_id(comment_id)
            src = "comment"
            if not row:
                row = db.find_search_comment_by_url(url) or db.find_search_comment_by_reddit_comment_id(comment_id)
                src = "search_comment"
            if row:
                post_brand_id = None
                if src == "comment" and row.get("post_id"):
                    pr = db.conn.execute("SELECT brand_id FROM posts WHERE id = ?", (row["post_id"],)).fetchone()
                    post_brand_id = pr["brand_id"] if pr else None
                elif src == "search_comment" and row.get("search_post_id"):
                    pr = db.conn.execute("SELECT brand_id FROM search_posts WHERE id = ?", (row["search_post_id"],)).fetchone()
                    post_brand_id = pr["brand_id"] if pr else None
                if not _owned(row, post_brand_id):
                    return jsonify({"found": False, "reason": "That link isn't in your reports."})
                if not row.get("report_month"):
                    return jsonify({"found": False, "reason": "That comment isn't in a monthly report yet."})
                return jsonify({"found": True, "month": row["report_month"],
                                "brand": _brand_name(row.get("brand_id") or post_brand_id),
                                "fid": comment_id, "kind": "comment"})
            # No comment row → fall through to post-level (the parent post may be a reported HQ).

        # --- Post link (or comment not matched): use the immutable post id ---
        post_id = parsed.get("post_id")
        for kind, post in db.find_posts_by_reddit_post_id(post_id):
            if kind == "post":
                r = db.conn.execute(
                    """SELECT report_month, COALESCE(report_client_id, -1) AS rcid
                         FROM comments
                        WHERE post_id = ? AND report_month IS NOT NULL
                          AND status IN ('report','removed','replace')
                        ORDER BY report_added_at DESC, id DESC LIMIT 1""",
                    (post["id"],)).fetchone()
                pbid = post.get("brand_id")
                if r and r["report_month"] and ((r["rcid"] != -1 and r["rcid"] == cid) or (r["rcid"] == -1 and pbid in brand_ids)):
                    return jsonify({"found": True, "month": r["report_month"],
                                    "brand": _brand_name(pbid), "fid": post_id, "kind": "hq"})
            else:  # search_post → its reported Mentions
                r = db.conn.execute(
                    """SELECT report_month, COALESCE(report_client_id, -1) AS rcid, brand_id
                         FROM search_comments
                        WHERE search_post_id = ? AND report_month IS NOT NULL
                          AND status IN ('report','removed','replace')
                        ORDER BY report_added_at DESC, id DESC LIMIT 1""",
                    (post["id"],)).fetchone()
                if r and r["report_month"]:
                    rbid = r["brand_id"] or post.get("brand_id")
                    if (r["rcid"] != -1 and r["rcid"] == cid) or (r["rcid"] == -1 and rbid in brand_ids):
                        return jsonify({"found": True, "month": r["report_month"],
                                        "brand": _brand_name(rbid), "fid": post_id, "kind": "mention"})
        return jsonify({"found": False, "reason": "That link isn't in your reports."})
    finally:
        db.close()


@app.route('/portal/month/<month>/export.csv')
@client_required
def portal_month_export(month):
    if not _valid_report_month(month):
        return abort(404)
    cid, _ = _acting_client_id()
    # Optional filters propagated from the in-page filter bar. The
    # frontend rebuilds the export URL on click to include whatever
    # is currently active, so the CSV mirrors what the user sees in
    # the table. All four params are case-insensitive and optional;
    # empty / missing = "all".
    #
    # `status` accepts 'report' (= Live, matches data-status on the
    # HQ row + search_comments.status='report'), 'removed', or
    # 'replace'. We normalise 'live' → 'report' for convenience so
    # external callers can use either label.
    brand_f = (request.args.get('brand') or '').strip()
    sub_f = (request.args.get('sub') or '').strip()
    status_f = (request.args.get('status') or '').strip().lower()
    if status_f == 'live':
        status_f = 'report'
    db = get_db()
    try:
        grouped = db.get_posts_for_client_month(cid, month)
    finally:
        db.close()

    # Apply the same filter predicates the in-page renderer uses
    # (templates/portal/month.html → applyFilters). Keeps the
    # exported rows in lockstep with what's visible on screen.
    def _post_passes(post):
        if brand_f and (post.get('brand_name') or '') != brand_f:
            return False
        if sub_f and (post.get('subreddit_name') or '') != sub_f:
            return False
        if status_f:
            derived = (post.get('derived_status') or 'live').lower()
            # data-status on HQ rows maps live→'report',
            # removed→'removed', replace→'replace'.
            row_status = 'report' if derived == 'live' else derived
            if row_status != status_f:
                return False
        return True

    def _mention_passes(c):
        if brand_f and (c.get('brand_name') or '') != brand_f:
            return False
        if sub_f and (c.get('subreddit_name') or '') != sub_f:
            return False
        if status_f and (c.get('status') or '') != status_f:
            return False
        return True

    grouped = {
        "posts": [p for p in (grouped.get("posts") or []) if _post_passes(p)],
        "search_comments": [c for c in (grouped.get("search_comments") or []) if _mention_passes(c)],
    }

    # Sectioned single-file CSV — mirrors the HTML layout:
    #   === HQ Mentions ===       (Live Subs posts, one row each)
    #     Subreddit | Published Date | Post Title | Post Link | Mention Link
    #   === Mentions ===          (Live Search comments, full column set)
    #     Subreddit | Published Date | Comment URL | Comment Content |
    #     Upvotes | Replies | Status
    # Sections with zero rows are omitted so the file is clean for
    # single-pipeline clients.
    def fmt_date(raw):
        if not raw:
            return ""
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', str(raw))
        return f"{m.group(2)}/{m.group(3)}/{m.group(1)}" if m else ""

    buf = _io.StringIO()
    w = _csv.writer(buf)

    posts_list = grouped.get("posts", []) or []
    search_list = grouped.get("search_comments", []) or []

    if posts_list:
        w.writerow(["=== HQ Mentions ==="])
        w.writerow([
            "Subreddit", "Published Date", "Post Title",
            "Post Link", "Mention Link", "Status",
        ])
        for post in posts_list:
            sub = f"r/{post.get('subreddit_name')}" if post.get('subreddit_name') else ''
            # posted_at = Reddit-side publish timestamp (preferred).
            # deployed_at / paid_at / created_at = internal stamps,
            # used only as fallback for legacy rows.
            published = fmt_date(
                post.get('posted_at')
                or post.get('deployed_at')
                or post.get('paid_at')
                or post.get('created_at')
            )
            derived = (post.get('derived_status') or 'live').lower()
            # 'replace' is the recent-removal sub-state — surface it
            # distinctly in the export so the client team can spot
            # rows that are eligible for redeploy.
            if derived == 'replace':
                status_label = 'Replace'
            elif derived == 'removed':
                status_label = 'Removed'
            else:
                status_label = 'Live'
            w.writerow([
                sub,
                published,
                post.get('title') or '',
                post.get('reddit_url') or '',
                post.get('mention_link') or '',
                status_label,
            ])

    if search_list:
        if posts_list:
            w.writerow([])  # blank separator row between sections
        w.writerow(["=== Mentions ==="])
        w.writerow([
            "Subreddit", "Published Date", "Comment URL",
            "Comment Content", "Upvotes", "Replies", "Status",
        ])
        for c in search_list:
            sub = f"r/{c.get('subreddit_name')}" if c.get('subreddit_name') else ''
            published = fmt_date(c.get('posted_at') or c.get('deployed_at'))
            _st = c.get('status') or ''
            if _st == 'report':
                status_label = 'Live'
            elif _st == 'replace':
                status_label = 'Replace'
            elif _st == 'removed':
                status_label = 'Removed'
            else:
                status_label = _st
            w.writerow([
                sub,
                published,
                c.get('reddit_comment_url') or '',
                c.get('body') or '',
                c.get('upvotes') if c.get('upvotes') is not None else '',
                c.get('num_replies') if c.get('num_replies') is not None else '',
                status_label,
            ])

    resp = make_response(buf.getvalue())
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename="report-{month}.csv"'
    return resp


def _build_check_live_items(rows, brand_filter=None):
    """Map reported-comment rows into the shape `_check_live_batch`
    expects. Optional `brand_filter` (case-insensitive name) narrows
    to a single brand — used when the live-check is scoped from a
    brand context."""
    items = []
    bf = (brand_filter or "").strip().lower() or None
    for r in rows:
        if bf and (r.get("brand_name") or "").strip().lower() != bf:
            continue
        items.append({
            "id": r["id"],
            "source": r.get("source", "comment"),
            "reddit_comment_url": r.get("reddit_comment_url"),
            "status": r.get("status"),
            "account_id": r.get("account_id"),
            "brand_name": r.get("brand_name"),
            "subreddit": r.get("subreddit_name"),
        })
    return items


@app.route('/portal/month/<month>/check-live', methods=['POST'])
@client_required
def portal_month_check_live(month):
    if not _valid_report_month(month):
        return abort(404)
    cid, _ = _acting_client_id()
    # Optional brand scope. When the user arrived at the month page
    # via a brand card, the URL carries ?brand=<name>; passing it
    # back here scopes the live-check to that brand's reports for
    # the month so we don't recheck every other brand's deliverables.
    brand_filter = (request.args.get('brand') or '').strip() or None
    if not brand_filter:
        try:
            body = request.get_json(silent=True) or {}
            brand_filter = (body.get('brand') or '').strip() or None
        except Exception:
            brand_filter = None

    def task(_task_id=None):
        bg = Database(DB_PATH)
        bg.connect(); bg.initialize()
        try:
            # Superset query: reported comments + every HQ root for
            # reported posts (even if the HQ root is still in
            # 'deployed' / 'paid' status). Without the HQ-root
            # extras, the dashboard would keep showing Live for
            # posts whose HQ comment is removed on Reddit but
            # never got refreshed because move_post_to_report
            # didn't flip its status.
            rows = bg.get_check_live_items_for_client_month(cid, month)
            items = _build_check_live_items(rows, brand_filter)
            return _check_live_batch(
                items, bg,
                log_prefix=f"PORTAL-CHECK-LIVE c={cid} m={month} brand={brand_filter or '*'}",
                task_id=_task_id,
            )
        finally:
            try: bg.close()
            except Exception: pass

    tid = start_task('portal-check-live', task, pass_task_id=True)
    return jsonify({"task_id": tid})


@app.route('/portal/brand/<int:bid>/check-live', methods=['POST'])
@client_required
def portal_brand_check_live(bid):
    """Run live-check on every reported comment that resolves to
    this brand across ALL months — scoped to the active client.
    Used by the brand-overview page so the admin can refresh just
    one brand without recomputing every other brand's status."""
    cid, _ = _acting_client_id()
    db = get_db()
    try:
        # Authorization: client must have this brand on their
        # client_brands row.
        if bid not in set(db.client_brand_ids(cid)):
            return abort(404)
        brand_row = db.conn.execute(
            "SELECT name FROM brands WHERE id = ?", (bid,)
        ).fetchone()
        brand_name = brand_row["name"] if brand_row else None
    finally:
        db.close()
    if not brand_name:
        return abort(404)

    def task(_task_id=None):
        bg = Database(DB_PATH)
        bg.connect(); bg.initialize()
        try:
            # Iterate every month the client has reports in, then
            # filter each month's rows to this brand. Reuses the
            # same per-month query so we get the same brand-
            # resolution chain — including the fallback to parent
            # post brand for unbranded comments.
            month_list = bg.get_report_months_for_client(cid)
            items = []
            for m in month_list:
                # Superset: reported comments + HQ roots tied to
                # reported posts. See note in
                # `portal_month_check_live` for why this is the
                # right scope.
                rows = bg.get_check_live_items_for_client_month(cid, m["month"])
                items.extend(_build_check_live_items(rows, brand_name))
            return _check_live_batch(
                items, bg,
                log_prefix=f"PORTAL-CHECK-LIVE c={cid} brand={brand_name}",
                task_id=_task_id,
            )
        finally:
            try: bg.close()
            except Exception: pass

    tid = start_task('portal-check-live-brand', task, pass_task_id=True)
    return jsonify({"task_id": tid})


@app.route('/portal/brand/<int:bid>/revert-last-check-live', methods=['POST'])
@client_required
def portal_brand_revert_last_check_live(bid):
    """One-click undo of this brand's MOST RECENT status update
    (check-live run). Brand-scoped: it can only revert rows tagged
    with this brand, so a concurrent run on another brand is never
    touched. Dry-run by default — pass {confirm:true} to apply.

    Returns {run, report}; `report` has the same shape as
    revert_check_live_window (reverted[]/skipped[]/...)."""
    cid, _ = _acting_client_id()
    confirm = bool((request.get_json(silent=True) or {}).get("confirm"))
    db = get_db()
    try:
        # Authorization: client must own this brand.
        if bid not in set(db.client_brand_ids(cid)):
            return abort(404)
        brand_row = db.conn.execute(
            "SELECT name FROM brands WHERE id = ?", (bid,)
        ).fetchone()
        brand_name = brand_row["name"] if brand_row else None
        if not brand_name:
            return abort(404)

        # Most recent check-live run for this brand (last 7 days).
        runs = db.get_check_live_log_runs(hours=168, brand_name=brand_name)
        if not runs:
            return jsonify({
                "run": None, "report": None,
                "error": "no recent status update found for this brand"})
        run = runs[0]
        report = db.revert_check_live_window(
            since=run["started_at"], until=run["ended_at"],
            brand_name=brand_name, dry_run=not confirm)
        return jsonify({"run": run, "report": report})
    finally:
        db.close()


@app.route('/portal/check-live-all', methods=['POST'])
@client_required
def portal_check_live_all():
    """Run live-check across every reported comment for the client
    (all brands, all months). The dashboard's global 'Update status'
    button calls this."""
    cid, _ = _acting_client_id()

    def task(_task_id=None):
        bg = Database(DB_PATH)
        bg.connect(); bg.initialize()
        try:
            month_list = bg.get_report_months_for_client(cid)
            items = []
            for m in month_list:
                # Superset query — see `portal_month_check_live`.
                rows = bg.get_check_live_items_for_client_month(cid, m["month"])
                items.extend(_build_check_live_items(rows))
            return _check_live_batch(
                items, bg,
                log_prefix=f"PORTAL-CHECK-LIVE-ALL c={cid}",
                task_id=_task_id,
            )
        finally:
            try: bg.close()
            except Exception: pass

    tid = start_task('portal-check-live-all', task, pass_task_id=True)
    return jsonify({"task_id": tid})


def _account_qs(is_admin, cid):
    """Build the ?as=... suffix used on every account-form action when
    the admin is viewing-as a client. Keeps the form posts on the
    correct client context."""
    return f"?as={cid}" if is_admin else ""


def _render_account(client, is_admin, error=None, success=None, section=None, status=200):
    """Single render path so every email/password endpoint can pass
    its own banner copy without duplicating the keyword set.
    `section` ('email' | 'password') tells the template which form's
    banner to highlight — handy when both forms live on the same page.
    """
    return render_template(
        'portal/account.html',
        client=client, is_admin_view=is_admin,
        error=error, success=success, section=section,
    ), status


@app.route('/portal/account', methods=['GET'])
@client_required
def portal_account():
    cid, is_admin = _acting_client_id()
    db = get_db()
    try:
        client = db.get_client(cid)
        resp, _ = _render_account(client, is_admin)
        return resp
    finally:
        db.close()


@app.route('/portal/account/password', methods=['POST'])
@client_required
def portal_change_password():
    cid, is_admin = _acting_client_id()
    current = request.form.get('current_password') or ''
    new = request.form.get('new_password') or ''
    confirm = request.form.get('confirm_password') or ''
    db = get_db()
    try:
        client = db.get_client(cid)
        # Admin in admin-view can change without knowing the current pw.
        if not is_admin:
            if not check_password_hash(client['password_hash'], current):
                resp, code = _render_account(client, is_admin,
                    error='Current password is incorrect.', section='password', status=401)
                return resp, code
        if not new or new != confirm or len(new) < 8:
            resp, code = _render_account(client, is_admin,
                error='New password must be at least 8 characters and match confirmation.',
                section='password', status=400)
            return resp, code
        db.update_client(cid, password_hash=generate_password_hash(new))
        client = db.get_client(cid)
        resp, _ = _render_account(client, is_admin,
            success='Password updated.', section='password')
        return resp
    finally:
        db.close()


# ----- Email management (add / remove / set-primary) -----------------------

@app.route('/portal/account/emails/add', methods=['POST'])
@client_required
def portal_account_email_add():
    cid, is_admin = _acting_client_id()
    email = (request.form.get('email') or '').strip()
    db = get_db()
    try:
        client = db.get_client(cid)
        ok, reason = db.add_client_email(cid, email)
        client = db.get_client(cid)
        if ok:
            resp, _ = _render_account(client, is_admin,
                success=f"Added {email}.", section='email')
            return resp
        copy = {
            'invalid':   "That doesn't look like a valid email address.",
            'taken':     "That email already belongs to another client account.",
            'duplicate': "That email is already on this account.",
        }.get(reason, 'Could not add email.')
        resp, code = _render_account(client, is_admin,
            error=copy, section='email', status=400)
        return resp, code
    finally:
        db.close()


@app.route('/portal/account/emails/remove', methods=['POST'])
@client_required
def portal_account_email_remove():
    cid, is_admin = _acting_client_id()
    email = (request.form.get('email') or '').strip()
    db = get_db()
    try:
        ok, reason = db.remove_client_email(cid, email)
        client = db.get_client(cid)
        if ok:
            resp, _ = _render_account(client, is_admin,
                success=f"Removed {email}.", section='email')
            return resp
        copy = {
            'not_found':  'That email is not on this account.',
            'last_email': "You can't remove the last email on the account — add a new one first.",
        }.get(reason, 'Could not remove email.')
        resp, code = _render_account(client, is_admin,
            error=copy, section='email', status=400)
        return resp, code
    finally:
        db.close()


@app.route('/portal/account/emails/primary', methods=['POST'])
@client_required
def portal_account_email_primary():
    cid, is_admin = _acting_client_id()
    email = (request.form.get('email') or '').strip()
    db = get_db()
    try:
        ok, reason = db.set_primary_email(cid, email)
        client = db.get_client(cid)
        if ok:
            resp, _ = _render_account(client, is_admin,
                success=f"{email} is now your primary contact.", section='email')
            return resp
        resp, code = _render_account(client, is_admin,
            error='That email is not on this account.', section='email', status=400)
        return resp, code
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    print(f"\nStarting web dashboard at http://localhost:{port}")
    app.run(debug=debug, port=port)
