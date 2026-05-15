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
# `gid` (the tab ID) can appear in the query string OR the URL fragment.
# Google's export endpoint accepts it as a query param, so we extract it
# from either location.
_SHEET_GID_RE = re.compile(r"[?&#]gid=([0-9]+)")


def extract_sheet_id(sheet_url: str) -> Optional[str]:
    """Return the sheet ID from a Google Sheets URL, or None.

    Accepts the canonical `/d/<ID>/...` form. Bare IDs (no `/d/`) are
    rejected to avoid mistakenly treating a Reddit URL as a sheet ID.
    """
    if not sheet_url or not isinstance(sheet_url, str):
        return None
    m = _SHEET_ID_RE.search(sheet_url)
    return m.group(1) if m else None


def extract_sheet_gid(sheet_url: str) -> Optional[str]:
    """Return the `gid` (tab ID) from a Google Sheets URL, or None.

    Google encodes the active tab as `?gid=<n>` or in the URL fragment
    `#gid=<n>`. Both forms are accepted. Without a gid the first tab
    is fetched — same as Google's UI when you open a sheet for the
    first time.
    """
    if not sheet_url or not isinstance(sheet_url, str):
        return None
    m = _SHEET_GID_RE.search(sheet_url)
    return m.group(1) if m else None


def fetch_sheet_csv(sheet_url: str, *, timeout: int = 20) -> str:
    """Download the specified tab of a Google Sheet as CSV.

    If the URL includes a `gid` (e.g. `?gid=12345` or `#gid=12345` —
    Google adds one whenever you click a tab in the UI), that tab is
    fetched. Otherwise we get the first tab.

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
    gid = extract_sheet_gid(sheet_url)
    export_url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    )
    if gid is not None:
        export_url += f"&gid={gid}"
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


# Canonical header names the bot looks for, in priority order. We only
# accept the sheet when at least one of these columns exists — that
# rules out accidentally picking up Reddit URLs from notes / pasted
# screenshots / template instructions elsewhere in the sheet.
_COMMENT_HEADER_NAMES = {"comment link", "comment url"}
_POST_HEADER_NAMES = {"post link", "post url"}


def _normalize_header(h):
    """Lowercase + collapse whitespace/dashes/underscores so 'Comment Link',
    'comment-link', 'Comment_Link', and 'comment  link' all compare equal.
    """
    if not h:
        return ""
    return re.sub(r"[\s_\-]+", " ", str(h).strip().lower())


def _find_url_columns(headers):
    """Locate the comment-link (and optional post-link) columns in the
    first row of the sheet. Returns
        {"comment": idx | None, "post": idx | None}

    The sheet must have at least one of these columns or
    `extract_reddit_urls` raises ValueError — better than silently
    pulling URLs from the wrong column.
    """
    out = {"comment": None, "post": None}
    for i, h in enumerate(headers or []):
        norm = _normalize_header(h)
        if out["comment"] is None and norm in _COMMENT_HEADER_NAMES:
            out["comment"] = i
        elif out["post"] is None and norm in _POST_HEADER_NAMES:
            out["post"] = i
    return out


def _extract_url_from_cell(cell):
    """Pull the first Reddit URL out of a single CSV cell.

    The cell may be a bare URL, a Google-Sheets `=HYPERLINK(...)` formula,
    a URL wrapped in quotes, or have surrounding whitespace. Returns the
    normalised URL (query string stripped, trailing slash removed) or
    None if no Reddit URL is present.
    """
    if not cell:
        return None
    m = _REDDIT_URL_RE.search(str(cell))
    if not m:
        return None
    url = m.group(0).rstrip(".,;)")
    return url.split("?")[0].rstrip("/")


def extract_reddit_urls(csv_text: str) -> list[str]:
    """Pull Reddit URLs out of a Google-Sheets CSV export.

    The sheet must have a header row with a column named "Comment Link"
    (case-insensitive; "comment url" / "comment-link" / "comment_link"
    are also accepted). An optional "Post Link" column is also read.
    URLs anywhere else in the sheet are ignored.

    Returns a deduped list of URLs preserving first-seen order. The
    order respects sheet order: row-by-row, comment column before post
    column.

    Raises ValueError when neither column is found — better than
    silently scanning the whole sheet and picking up unrelated URLs.
    """
    if not csv_text:
        return []
    import csv as _csv
    import io as _io
    reader = _csv.reader(_io.StringIO(csv_text))
    try:
        headers = next(reader)
    except StopIteration:
        return []
    cols = _find_url_columns(headers)
    if cols["comment"] is None and cols["post"] is None:
        raise ValueError(
            "Sheet must have a column named 'Comment Link' (or 'Post Link'). "
            "Header row read: " + ", ".join(repr(h) for h in headers[:8])
            + ("..." if len(headers) > 8 else "")
        )
    found = []
    seen = set()
    for row in reader:
        if not row:
            continue
        # For each row we look at the comment-link column first, then
        # the post-link column. This keeps the output order intuitive
        # when both columns are populated on the same row.
        for kind in ("comment", "post"):
            idx = cols[kind]
            if idx is None or idx >= len(row):
                continue
            url = _extract_url_from_cell(row[idx])
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

def _utc_seconds_to_iso(secs) -> Optional[str]:
    """Convert a Reddit `created_utc` Unix-seconds float into the
    "YYYY-MM-DD HH:MM:SS" UTC string we store everywhere else.
    Returns None for 0 / missing / non-numeric inputs (Reddit
    occasionally omits the field on deleted comments).
    """
    try:
        secs = float(secs)
    except (TypeError, ValueError):
        return None
    if secs <= 0:
        return None
    return datetime.utcfromtimestamp(secs).strftime("%Y-%m-%d %H:%M:%S")


def fetch_reddit_comment_meta(comment_url: str, *, reddit_get: Callable,
                               timeout: int = 15) -> dict:
    """Fetch the target comment's metadata from Reddit.

    Returns:
        {
          "body": str | None,
          "posted_at": "YYYY-MM-DD HH:MM:SS" | None,
          "is_removed": bool,
          "found": bool,
        }

    `is_removed` is True when the body is `"[removed]"` / `"[deleted]"`
    (mod removal or user deletion). `found` is True when the target
    comment ID was located in the JSON response at all; False on
    fetch error, URL-parse failure, or 404. The two flags let the
    caller distinguish "live" / "removed" / "missing" without
    re-walking the body.

    `reddit_get` is the app's `_reddit_get` (proxy + retry).
    """
    from urllib.parse import urlparse
    out = {"body": None, "posted_at": None, "is_removed": False, "found": False}
    try:
        parsed = urlparse(comment_url)
        path = parsed.path.rstrip("/") + ".json"
    except Exception:
        return out
    try:
        data = reddit_get(path)
    except Exception:
        return out
    if not data:
        return out
    parsed_info = classify_reddit_url(comment_url)
    if not parsed_info:
        return out
    target_cid = (parsed_info.get("comment_id") or "").lower()
    if not target_cid:
        return out

    def _walk(node):
        """Yield (id, body, created_utc) for every comment in the tree."""
        if isinstance(node, dict):
            kind = node.get("kind")
            if kind == "Listing":
                for child in (node.get("data") or {}).get("children", []) or []:
                    yield from _walk(child)
            elif kind == "t1":
                d = node.get("data") or {}
                yield (
                    d.get("id", "").lower(),
                    d.get("body", ""),
                    d.get("created_utc", 0),
                )
                replies = d.get("replies")
                if isinstance(replies, dict):
                    yield from _walk(replies)
        elif isinstance(node, list):
            for el in node:
                yield from _walk(el)

    for cid, body, created_utc in _walk(data):
        if cid == target_cid:
            out["body"] = body
            out["posted_at"] = _utc_seconds_to_iso(created_utc)
            out["found"] = True
            stripped = (body or "").strip().lower()
            out["is_removed"] = stripped in ("[removed]", "[deleted]")
            return out
    return out


def classify_liveness(meta: dict) -> str:
    """Map a fetch_reddit_comment_meta result to a coarse liveness string.

    Returns one of:
      - "live"    — body present, not a sentinel
      - "removed" — comment exists on Reddit but was removed/deleted
      - "missing" — the comment couldn't be located (fetch failed,
                    404, URL malformed, etc.)
    """
    if not meta:
        return "missing"
    if not meta.get("found"):
        return "missing"
    if meta.get("is_removed"):
        return "removed"
    return "live"


def fetch_reddit_comment_body(comment_url: str, *, reddit_get: Callable,
                              timeout: int = 15) -> Optional[str]:
    """Backwards-compat wrapper. New callers should use
    `fetch_reddit_comment_meta` which returns body + posted_at in one
    call (same network cost — one JSON fetch).
    """
    return fetch_reddit_comment_meta(
        comment_url, reddit_get=reddit_get, timeout=timeout
    ).get("body")


# ---------------------------------------------------------------------------
# Per-URL matcher
# ---------------------------------------------------------------------------

# Statuses that count as "already finished" — no need to re-deploy.
_DONE_STATUSES = {"deployed", "paid", "archived"}


def _row_context(db, comment_id, kind):
    """Look up subreddit + brand + account for a row so log entries
    populate the Activity Log columns. Best-effort; returns (None,
    None, None) on any failure — the log row still writes, just with
    less context.
    """
    try:
        if kind == "comment":
            row = db.conn.execute(
                """SELECT s.name AS subreddit, b.name AS brand, c.account_id
                   FROM comments c
                   JOIN posts p ON c.post_id = p.id
                   LEFT JOIN subreddits s ON p.subreddit_id = s.id
                   LEFT JOIN brands b ON c.brand_id = b.id
                   WHERE c.id = ?""",
                (comment_id,)
            ).fetchone()
        elif kind == "search_comment":
            row = db.conn.execute(
                """SELECT sp.subreddit AS subreddit, b.name AS brand, sc.account_id
                   FROM search_comments sc
                   JOIN search_posts sp ON sc.search_post_id = sp.id
                   LEFT JOIN brands b ON sc.brand_id = b.id
                   WHERE sc.id = ?""",
                (comment_id,)
            ).fetchone()
        else:
            row = None
        if row:
            # SQLite Row → indexable by column name when row_factory is set,
            # but defensively coerce via try/except.
            try:
                return row["subreddit"], row["brand"], row["account_id"]
            except Exception:
                return None, None, None
    except Exception:
        pass
    return None, None, None


def _log_bulk_event(db, comment_id, kind, reddit_url, action,
                     prev_status, new_status):
    """Best-effort write to check_live_log. We swallow failures because
    a logging hiccup must NEVER undo the user's actual deploy/remove
    action. The Activity Log is informational, not transactional.
    """
    subreddit, brand_name, account_id = _row_context(db, comment_id, kind)
    try:
        db.log_live_check(
            comment_id=comment_id,
            source=kind,
            reddit_url=reddit_url,
            action=action,
            prev_status=prev_status or "",
            new_status=new_status or "",
            account_id=account_id,
            subreddit=subreddit,
            brand_name=brand_name,
        )
    except Exception as e:
        print(f"[bulk-deploy] log_live_check failed: {e}", flush=True)


def match_and_deploy_comment(db, classified: dict, *,
                              reddit_get: Callable,
                              similarity_threshold: float = 0.5) -> dict:
    """Process a single comment URL — Tier 1 → Tier 2 → no-match.

    Returns a result dict the orchestrator records in tasks.progress.
    Possible action values:
      - "deployed"            (URL/body match — flipped to deployed)
      - "marked_removed"      (URL matched but Reddit shows removed/deleted)
      - "already_deployed"    (idempotent — was already deployed)
      - "already_removed"     (idempotent — was already removed)
      - "no_match"            (URL didn't map to any DB row)
      - "error"               (transient failure — see "reason")

    Adds `liveness` ("live" | "removed" | "missing") whenever we did
    fetch the Reddit JSON.
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
        current_status = row.get("status")
        # Already done in our DB.
        if current_status in _DONE_STATUSES:
            return {
                "url": url, "kind": "comment", "tier": "url_match",
                "source": source, "id": row["id"],
                "action": "already_deployed",
            }
        if current_status == "removed":
            # Patch URL / posted_at in case they were missing, but don't
            # flip status or write a log row.
            meta = fetch_reddit_comment_meta(url, reddit_get=reddit_get)
            try:
                db.mark_removed_with_url(
                    row["id"], source, url, posted_at=meta.get("posted_at"),
                )
            except Exception:
                pass
            return {
                "url": url, "kind": "comment", "tier": "url_match",
                "source": source, "id": row["id"],
                "action": "already_removed",
                "liveness": classify_liveness(meta),
            }
        # One Reddit fetch buys us liveness + posted_at simultaneously.
        meta = fetch_reddit_comment_meta(url, reddit_get=reddit_get)
        posted_at = meta.get("posted_at")
        liveness = classify_liveness(meta)
        if liveness == "removed":
            # Comment was posted on Reddit, but is no longer live.
            # Attach URL + flip the DB row to 'removed' (instead of
            # 'deployed') and log the activity.
            try:
                prev = db.mark_removed_with_url(
                    row["id"], source, url, posted_at=posted_at,
                )
            except Exception as e:
                return {
                    "url": url, "kind": "comment", "tier": "url_match",
                    "source": source, "id": row["id"],
                    "action": "error", "reason": str(e),
                    "liveness": liveness,
                }
            _log_bulk_event(
                db, row["id"], source, url,
                "bulk_marked_removed", prev, "removed",
            )
            return {
                "url": url, "kind": "comment", "tier": "url_match",
                "source": source, "id": row["id"],
                "action": "marked_removed",
                "liveness": liveness, "posted_at": posted_at,
            }
        # liveness in {"live", "missing"} — keep existing deploy path.
        # "missing" still flips to deployed: the user explicitly told us
        # via the sheet that this URL is correct; Reddit just couldn't
        # confirm it right now (rate limit / proxy hiccup). The
        # backfill / live-check will pick the row up later if it turns
        # out the comment was actually removed.
        prev_status = (row.get("status") or "") if isinstance(row, dict) else ""
        try:
            if source == "comment":
                db.deploy_comment(row["id"], url, posted_at=posted_at)
            else:
                db.deploy_search_comment(row["id"], url, posted_at=posted_at)
            _log_bulk_event(
                db, row["id"], source, url,
                "bulk_deployed", prev_status, "deployed",
            )
            return {
                "url": url, "kind": "comment", "tier": "url_match",
                "source": source, "id": row["id"], "action": "deployed",
                "posted_at": posted_at, "liveness": liveness,
            }
        except Exception as e:
            return {
                "url": url, "kind": "comment", "tier": "url_match",
                "source": source, "id": row["id"],
                "action": "error", "reason": str(e),
                "liveness": liveness,
            }

    # --- Tier 2: post + body fuzzy match ----------------------------------
    # Match the parent post by its Reddit post ID (the immutable
    # `/comments/<id>/` segment) rather than by the full URL. URLs in
    # the user's sheet often use Reddit's `/comment/` placeholder slug
    # (e.g. `/r/sub/comments/POSTID/comment/CMTID/`) while our DB stores
    # the actual title slug. Matching on the post-ID substring lets us
    # bridge that without forcing the user to massage their URLs.
    post_kind, post_row = db.find_post_by_reddit_post_id(classified["post_id"])
    if not post_row:
        return {
            "url": url, "kind": "comment", "action": "no_match",
            "reason": (
                "parent post (post_id=" + str(classified["post_id"])
                + ") not found in posts or search_posts"
            ),
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

    # One JSON fetch gives us body (for matching), posted_at (for the
    # column) AND liveness — no extra round trip.
    meta = fetch_reddit_comment_meta(url, reddit_get=reddit_get)
    body = meta.get("body")
    posted_at = meta.get("posted_at")
    liveness = classify_liveness(meta)

    if liveness == "missing" or body is None:
        return {
            "url": url, "kind": "comment", "action": "no_match",
            "reason": "reddit_fetch_failed_or_comment_unreachable",
            "liveness": liveness,
        }

    if liveness == "removed":
        # Comment was removed/deleted on Reddit. We can't body-match
        # against a `[removed]` sentinel, so we fall back to a
        # singleton rule: if exactly ONE undeployed comment sits under
        # this post, claim the URL goes to it, attach URL, mark removed.
        # Otherwise we report no_match — never guess between candidates.
        if len(candidates) == 1:
            cand = candidates[0]
            try:
                prev = db.mark_removed_with_url(
                    cand["id"], candidate_kind, url, posted_at=posted_at,
                )
            except Exception as e:
                return {
                    "url": url, "kind": "comment", "tier": "removed_singleton",
                    "source": candidate_kind, "id": cand["id"],
                    "action": "error", "reason": str(e),
                    "liveness": liveness,
                }
            _log_bulk_event(
                db, cand["id"], candidate_kind, url,
                "bulk_marked_removed", prev, "removed",
            )
            return {
                "url": url, "kind": "comment", "tier": "removed_singleton",
                "source": candidate_kind, "id": cand["id"],
                "action": "marked_removed",
                "liveness": liveness, "posted_at": posted_at,
            }
        return {
            "url": url, "kind": "comment", "action": "no_match",
            "reason": f"comment_removed_on_reddit_and_{len(candidates)}_candidates",
            "liveness": liveness,
        }

    # liveness == "live" — body fuzzy match as before.
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
            "liveness": liveness,
        }
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    ambiguous = runner_up >= similarity_threshold and (best_score - runner_up) < 0.1
    prev_status = best.get("status") if isinstance(best, dict) else None
    try:
        if candidate_kind == "comment":
            db.deploy_comment(best["id"], url, posted_at=posted_at)
        else:
            db.deploy_search_comment(best["id"], url, posted_at=posted_at)
    except Exception as e:
        return {
            "url": url, "kind": "comment", "tier": "body_match",
            "source": candidate_kind, "id": best["id"],
            "action": "error", "reason": str(e),
            "liveness": liveness,
        }
    # Audit trail — one row per bulk-deploy event. Lets the user verify
    # via Activity Log that the write actually happened, independent of
    # any UI cache.
    _log_bulk_event(
        db, best["id"], candidate_kind, url,
        "bulk_deployed", prev_status or "", "deployed",
    )
    out = {
        "url": url, "kind": "comment", "tier": "body_match",
        "source": candidate_kind, "id": best["id"], "action": "deployed",
        "similarity": round(best_score, 3),
        "posted_at": posted_at, "liveness": liveness,
    }
    if ambiguous:
        out["ambiguous"] = True
        out["runner_up_similarity"] = round(runner_up, 3)
    return out


def match_and_deploy_post(db, classified: dict) -> dict:
    """Process a single post URL — match by Reddit post id so slug
    differences (e.g. user copied the URL with a different title slug
    than the one we have stored) don't cause a miss.
    """
    url = classified["url"]
    post_kind, post_row = db.find_post_by_reddit_post_id(classified["post_id"])
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
        "marked_removed": 0,
        "already_deployed": 0,
        "already_removed": 0,
        "no_match": 0,
        "error": 0,
        "by_tier": {"url_match": 0, "body_match": 0, "removed_singleton": 0},
        "by_liveness": {"live": 0, "removed": 0, "missing": 0},
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
        liveness = r.get("liveness")
        if liveness in counts["by_liveness"]:
            counts["by_liveness"][liveness] += 1
    return counts
