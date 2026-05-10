"""Bulk Deploy from Google Sheet.

The user drops a Google Sheet link into the app; the sheet has a mixed
list of Reddit URLs (post and comment URLs across multiple brands); the
bot walks every URL one by one, matches each to its DB row, and marks
it deployed with the URL stored.

This module is the orchestration layer. It is intentionally self-
contained — every external call (DB, Reddit fetch, sheet download) is
either passed in as a callable or accessed through a thin wrapper so
unit tests can stub them without spinning up the whole app.

Matching strategy (per the approved plan):
  - Tier 1: direct `reddit_comment_url = ?` lookup. Hits when the user
    pre-pasted the URL via the existing "informed" stage.
  - Tier 2: locate the parent post by URL, fetch the comment body from
    Reddit's JSON endpoint, fuzzy-match against undeployed comments on
    that post. Threshold ≥ 0.5 Jaccard.
  - Tier 3: no match — report and move on. We never invent a row.

Post URLs (no comment ID) use direct lookup only — either `post_urls`
(legacy) or `search_posts.reddit_url` (Live Search).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Google Sheets ingestion
# ---------------------------------------------------------------------------

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]{20,})")


def extract_sheet_id(sheet_url: str) -> Optional[str]:
    """Return the sheet ID from a Google Sheets URL, or None.

    Accepts the canonical `/d/<ID>/...` form. Bare IDs (no `/d/`) are
    rejected to avoid mistakenly treating a Reddit URL as a sheet ID.
    """
    if not sheet_url or not isinstance(sheet_url, str):
        return None
    m = _SHEET_ID_RE.search(sheet_url)
    return m.group(1) if m else None


def fetch_sheet_csv(sheet_url: str, *, timeout: int = 20) -> str:
    """Download a Google Sheet's first tab as CSV.

    Requires the sheet to be set to 'Anyone with the link → Viewer'.
    Raises ValueError with an actionable message if the sheet ID can't
    be parsed or the export endpoint returns non-CSV (auth wall, 401,
    deleted sheet, etc.).
    """
    import requests
    sheet_id = extract_sheet_id(sheet_url)
    if not sheet_id:
        raise ValueError(
            "Sheet URL is not a recognised Google Sheets link. "
            "Use the full URL that contains /spreadsheets/d/<ID>/..."
        )
    export_url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    )
    try:
        resp = requests.get(export_url, timeout=timeout, allow_redirects=True)
    except requests.RequestException as e:
        raise ValueError(f"Sheet fetch failed: {e}")
    if resp.status_code != 200:
        raise ValueError(
            f"Sheet fetch returned HTTP {resp.status_code}. "
            "Set the sheet to 'Anyone with the link → Viewer' and try again."
        )
    ctype = (resp.headers.get("Content-Type") or "").lower()
    # Google redirects authenticated-only sheets to a login page. The body
    # comes back as text/html instead of text/csv — detect that and tell
    # the user.
    if "text/csv" not in ctype and "text/plain" not in ctype:
        raise ValueError(
            "Sheet is not publicly readable — Google returned an HTML "
            "page instead of CSV. Set the sheet to "
            "'Anyone with the link → Viewer' and try again."
        )
    return resp.text


# ---------------------------------------------------------------------------
# Reddit URL extraction + classification
# ---------------------------------------------------------------------------

# Permissive Reddit URL matcher: accepts http/https, any reddit.com
# subdomain, and the /r/<sub>/comments/<post_id>(/<slug>(/<comment_id>)?)? form.
_REDDIT_URL_RE = re.compile(
    r"https?://(?:www\.|old\.|new\.|np\.|m\.)?reddit\.com"
    r"/r/[A-Za-z0-9_]+/comments/[A-Za-z0-9]+"
    r"(?:/[^/\s,;\"'<>]*)?"
    r"(?:/[A-Za-z0-9]{4,12})?",
    re.IGNORECASE,
)


def extract_reddit_urls(csv_text: str) -> list[str]:
    """Scan a CSV/raw-text blob for Reddit URLs.

    Returns a deduped list preserving first-seen order. We don't bother
    with proper CSV parsing because the user may have URLs in any
    column or mixed in with prose — a regex scan is more robust.
    """
    if not csv_text:
        return []
    found = []
    seen = set()
    for m in _REDDIT_URL_RE.finditer(csv_text):
        url = m.group(0).rstrip(".,;)")
        # Drop trailing query strings; we treat the path as the identity.
        url = url.split("?")[0].rstrip("/")
        if url and url not in seen:
            seen.add(url)
            found.append(url)
    return found


# Parse the Reddit URL into its parts. We mirror the patterns used in
# `app.py:_normalize_reddit_comment_url` for consistency.
_REDDIT_URL_PARSE_RE = re.compile(
    r"^https?://(?:www\.|old\.|new\.|np\.|m\.)?reddit\.com"
    r"/r/(?P<sub>[A-Za-z0-9_]+)"
    r"/comments/(?P<post_id>[A-Za-z0-9]+)"
    r"(?:/(?P<slug>[^/]*))?"
    r"(?:/(?P<comment_id>[A-Za-z0-9]{4,12}))?"
    r"/?$",
    re.IGNORECASE,
)


def classify_reddit_url(url: str) -> Optional[dict]:
    """Parse a Reddit URL into structured fields.

    Returns:
        {
          "kind": "comment" | "post",
          "sub": str,
          "post_id": str,
          "comment_id": str | None,
          "url": str,                    # the input URL, trimmed
          "post_url": str,               # URL of the parent post (no comment ID)
        }
    or None when the URL doesn't match the expected pattern (e.g. /s/
    share links, redd.it shorteners, or junk strings).
    """
    if not url:
        return None
    url = url.split("?")[0].rstrip("/")
    m = _REDDIT_URL_PARSE_RE.match(url)
    if not m:
        return None
    sub = m.group("sub")
    post_id = m.group("post_id")
    slug = m.group("slug") or ""
    comment_id = m.group("comment_id")
    # If the URL has no slug, "comment_id" may have actually been the slug
    # in disguise. The regex requires comment IDs to be 4-12 chars, which
    # excludes typical slugs (much longer), but a short slug could still
    # confuse the parser. We use a simple rule: a real comment ID is
    # alphanumeric, 4-12 chars, AND there must be a slug between
    # post_id and comment_id. If there's no slug, treat the trailing
    # segment as a slug, not a comment.
    if comment_id and not slug:
        # Looks like /r/sub/comments/POSTID/X — X is a slug, not a comment
        comment_id = None
    kind = "comment" if comment_id else "post"
    # Reconstruct the post URL (no comment ID) for Tier-2 lookup.
    if slug:
        post_url = f"https://www.reddit.com/r/{sub}/comments/{post_id}/{slug}"
    else:
        post_url = f"https://www.reddit.com/r/{sub}/comments/{post_id}"
    return {
        "kind": kind,
        "sub": sub,
        "post_id": post_id,
        "comment_id": comment_id,
        "url": url,
        "post_url": post_url,
    }


# ---------------------------------------------------------------------------
# Tier-2 body similarity (cheap Jaccard on lowercased word sets)
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"\w+")


def _word_set(s: str) -> set[str]:
    if not s:
        return set()
    return {w.lower() for w in _WORD_RE.findall(s) if len(w) >= 2}


def jaccard(a: str, b: str) -> float:
    """Jaccard similarity over lowercased word multisets (sets here).

    Empty inputs → 0.0. Returns a value in [0.0, 1.0].
    """
    aw, bw = _word_set(a), _word_set(b)
    if not aw or not bw:
        return 0.0
    inter = aw & bw
    union = aw | bw
    return len(inter) / len(union)


# ---------------------------------------------------------------------------
# Reddit comment body fetcher
# ---------------------------------------------------------------------------

def fetch_reddit_comment_body(comment_url: str, *, reddit_get: Callable,
                              timeout: int = 15) -> Optional[str]:
    """Fetch the body of a Reddit comment given its full URL.

    `reddit_get` is a callable that takes a path (e.g. `/r/sub/comments/.../id/.json`)
    and returns the parsed JSON list/dict the Reddit endpoint produces.
    We inject it so app.py's `_reddit_get` (proxy-aware, retry-aware)
    can be plugged in without re-implementing it here.

    Returns the comment body text, or None if the URL can't be parsed,
    the fetch fails, or the comment can't be located in the response.
    """
    from urllib.parse import urlparse
    try:
        parsed = urlparse(comment_url)
        path = parsed.path.rstrip("/") + ".json"
    except Exception:
        return None
    try:
        data = reddit_get(path)
    except Exception:
        return None
    if not data:
        return None
    # Reddit's comment endpoint returns a 2-element list: [post_listing,
    # comments_listing]. Walk the comments listing for the target body.
    # The target comment ID is the last path segment (before .json).
    parsed_info = classify_reddit_url(comment_url)
    if not parsed_info:
        return None
    target_cid = (parsed_info.get("comment_id") or "").lower()
    if not target_cid:
        return None

    def _walk(node):
        """Recursively yield (id, body) for every comment in the tree."""
        if isinstance(node, dict):
            kind = node.get("kind")
            if kind == "Listing":
                for child in (node.get("data") or {}).get("children", []) or []:
                    yield from _walk(child)
            elif kind == "t1":
                d = node.get("data") or {}
                yield (d.get("id", "").lower(), d.get("body", ""))
                replies = d.get("replies")
                if isinstance(replies, dict):
                    yield from _walk(replies)
        elif isinstance(node, list):
            for el in node:
                yield from _walk(el)

    # Prefer the comment listing (index 1) but walk the whole response
    # defensively — some Reddit responses interleave.
    for cid, body in _walk(data):
        if cid == target_cid:
            return body
    return None


# ---------------------------------------------------------------------------
# Per-URL matcher
# ---------------------------------------------------------------------------

# Statuses that count as "already finished" — no need to re-deploy.
_DONE_STATUSES = {"deployed", "paid", "archived"}


def match_and_deploy_comment(db, classified: dict, *,
                              reddit_get: Callable,
                              similarity_threshold: float = 0.5) -> dict:
    """Process a single comment URL — Tier 1 → Tier 2 → no-match.

    Returns a result dict the orchestrator records in tasks.progress.
    Possible action values:
      - "deployed"            (tier_url / tier_body — flipped to deployed)
      - "already_deployed"    (idempotent — was already deployed)
      - "no_match"            (URL didn't map to any DB row)
      - "error"               (transient failure — see "reason")
    """
    url = classified["url"]

    # --- Tier 1: direct URL match -----------------------------------------
    row = db.find_comment_by_url(url)
    source = "comment"
    if not row:
        row = db.find_search_comment_by_url(url)
        if row:
            source = "search_comment"
    if row:
        if row.get("status") in _DONE_STATUSES:
            return {
                "url": url, "kind": "comment", "tier": "url_match",
                "source": source, "id": row["id"],
                "action": "already_deployed",
            }
        try:
            if source == "comment":
                db.deploy_comment(row["id"], url)
            else:
                db.deploy_search_comment(row["id"], url)
            return {
                "url": url, "kind": "comment", "tier": "url_match",
                "source": source, "id": row["id"], "action": "deployed",
            }
        except Exception as e:
            return {
                "url": url, "kind": "comment", "tier": "url_match",
                "source": source, "id": row["id"],
                "action": "error", "reason": str(e),
            }

    # --- Tier 2: post + body fuzzy match ----------------------------------
    post_kind, post_row = db.find_post_by_url(classified["post_url"])
    if not post_row:
        return {
            "url": url, "kind": "comment", "action": "no_match",
            "reason": "parent post URL not found in posts or search_posts",
        }
    candidate_kind = "comment" if post_kind == "post" else "search_comment"
    candidates = db.find_undeployed_comments_for_post(
        post_row["id"], candidate_kind
    )
    if not candidates:
        return {
            "url": url, "kind": "comment", "action": "no_match",
            "reason": "no undeployed comments under that post",
        }

    body = fetch_reddit_comment_body(url, reddit_get=reddit_get)
    if not body:
        return {
            "url": url, "kind": "comment", "action": "no_match",
            "reason": "reddit_fetch_failed_or_comment_unreachable",
        }
    if body.strip().lower() in ("[deleted]", "[removed]"):
        return {
            "url": url, "kind": "comment", "action": "no_match",
            "reason": "comment_body_deleted_or_removed_on_reddit",
        }

    scored = sorted(
        ((jaccard(body, c["body"]), c) for c in candidates),
        key=lambda p: -p[0],
    )
    best_score, best = scored[0]
    if best_score < similarity_threshold:
        return {
            "url": url, "kind": "comment", "action": "no_match",
            "reason": f"best body similarity {best_score:.2f} below threshold {similarity_threshold:.2f}",
            "best_id": best["id"], "best_score": round(best_score, 3),
        }
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    ambiguous = runner_up >= similarity_threshold and (best_score - runner_up) < 0.1
    try:
        if candidate_kind == "comment":
            db.deploy_comment(best["id"], url)
        else:
            db.deploy_search_comment(best["id"], url)
    except Exception as e:
        return {
            "url": url, "kind": "comment", "tier": "body_match",
            "source": candidate_kind, "id": best["id"],
            "action": "error", "reason": str(e),
        }
    out = {
        "url": url, "kind": "comment", "tier": "body_match",
        "source": candidate_kind, "id": best["id"], "action": "deployed",
        "similarity": round(best_score, 3),
    }
    if ambiguous:
        out["ambiguous"] = True
        out["runner_up_similarity"] = round(runner_up, 3)
    return out


def match_and_deploy_post(db, classified: dict) -> dict:
    """Process a single post URL — direct lookup in posts / search_posts."""
    url = classified["url"]
    post_kind, post_row = db.find_post_by_url(url)
    if not post_row:
        return {
            "url": url, "kind": "post", "action": "no_match",
            "reason": "post URL not found in posts or search_posts",
        }
    if post_row.get("status") == "deployed":
        return {
            "url": url, "kind": "post", "source": post_kind,
            "id": post_row["id"], "action": "already_deployed",
        }
    try:
        ok = db.deploy_post(
            post_row["id"], post_kind, url,
            subreddit_id=post_row.get("subreddit_id"),
        )
    except Exception as e:
        return {
            "url": url, "kind": "post", "source": post_kind,
            "id": post_row["id"], "action": "error", "reason": str(e),
        }
    return {
        "url": url, "kind": "post", "source": post_kind,
        "id": post_row["id"],
        "action": "deployed" if ok else "already_deployed",
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_bulk_deploy(sheet_url: str, *, db_factory: Callable,
                     reddit_get: Callable,
                     _task_id: Optional[str] = None) -> dict:
    """Walk every URL in the sheet, match each to a DB row, mark deployed.

    Designed to be called via app.py's `start_task(...,
    pass_task_id=True)` — `_task_id` is injected by `start_task`.

    `db_factory` is a callable returning a fresh `Database` (we want a
    new connection per background thread, not the request-thread's
    connection). `reddit_get` is the app's `_reddit_get` (proxy +
    retry).

    Returns the final report dict; also written to tasks.progress
    incrementally so the UI can poll `GET /api/tasks/<id>`.
    """
    # Fetch + parse the sheet first — fail fast if it's not public.
    csv_text = fetch_sheet_csv(sheet_url)
    urls = extract_reddit_urls(csv_text)

    report = {
        "total": len(urls),
        "processed": 0,
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "results": [],
    }

    # Open a dedicated DB connection for the task thread.
    db = db_factory()
    try:
        for url in urls:
            classified = classify_reddit_url(url)
            if not classified:
                row = {
                    "url": url, "action": "no_match",
                    "reason": "URL didn't parse as a Reddit post/comment",
                }
            elif classified["kind"] == "comment":
                row = match_and_deploy_comment(
                    db, classified, reddit_get=reddit_get,
                )
            else:
                row = match_and_deploy_post(db, classified)
            report["results"].append(row)
            report["processed"] += 1
            if _task_id:
                try:
                    db.update_task_progress(_task_id, json.dumps(report))
                except Exception:
                    # Progress update is best-effort; don't kill the
                    # main run because the progress write failed.
                    pass
        report["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Summary counts for quick UI rendering.
        report["summary"] = _summarise(report["results"])
        return report
    finally:
        try:
            db.close()
        except Exception:
            pass


def _summarise(results: list[dict]) -> dict:
    """Roll the per-row results into top-line counts."""
    counts = {
        "deployed": 0,
        "already_deployed": 0,
        "no_match": 0,
        "error": 0,
        "by_tier": {"url_match": 0, "body_match": 0},
        "comments": 0,
        "posts": 0,
    }
    for r in results:
        action = r.get("action")
        if action in counts:
            counts[action] += 1
        if r.get("kind") == "comment":
            counts["comments"] += 1
        elif r.get("kind") == "post":
            counts["posts"] += 1
        tier = r.get("tier")
        if tier in counts["by_tier"]:
            counts["by_tier"][tier] += 1
    return counts
