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

# Two Reddit comment URL formats we accept from the sheet:
#
# 1. Full /comments/<post_id>/<slug>/<comment_id> form — preserved as-is.
# 2. Short share form /r/<sub>/s/<token> — Reddit's "Share" dialog spits
#    these out. They redirect to the canonical form when followed. We
#    pick them up here so the orchestrator can resolve them before
#    matching, instead of silently dropping them.
_REDDIT_URL_RE = re.compile(
    r"https?://(?:www\.|old\.|new\.|np\.|m\.)?reddit\.com"
    r"/r/[A-Za-z0-9_]+"
    r"(?:"
    r"/comments/[A-Za-z0-9]+(?:/[^/\s,;\"'<>]*)?(?:/[A-Za-z0-9]{4,12})?"
    r"|"
    r"/s/[A-Za-z0-9_-]+"
    r")",
    re.IGNORECASE,
)

_REDDIT_SHARE_RE = re.compile(
    r"^https?://(?:www\.|old\.|new\.|np\.|m\.)?reddit\.com/r/[A-Za-z0-9_]+/s/[A-Za-z0-9_-]+/?$",
    re.IGNORECASE,
)


# Canonical header names. Only "Comment Link" (or its case/separator
# variants) is read. "Post Link" / other columns are deliberately
# IGNORED — those URLs are usually for human readability, not for
# deployment. Reading them caused the modal to report dozens of false
# "already_deployed" entries (the search_posts rows were already in a
# terminal state, so the post matcher kept saying "already_deployed").
_COMMENT_HEADER_NAMES = {"comment link", "comment url"}
# Headers that carry an explicit comment ID assigned by the operator
# (rather than the one we'd parse from the URL). The Check Live tool
# uses this value verbatim in its report so the operator can match
# their sheet's row identifiers to the liveness verdict.
_COMMENT_ID_HEADER_NAMES = {
    "comment id", "id", "cid", "comment_id", "row id",
}


def _normalize_header(h):
    """Lowercase + collapse whitespace/dashes/underscores so 'Comment Link',
    'comment-link', 'Comment_Link', and 'comment  link' all compare equal.
    """
    if not h:
        return ""
    return re.sub(r"[\s_\-]+", " ", str(h).strip().lower())


def _find_comment_column(headers):
    """Return the 0-indexed column matching a Comment-Link header,
    or None. Only `comment link` (and its case/separator variants) is
    accepted — post-link / other columns are intentionally ignored.
    """
    for i, h in enumerate(headers or []):
        if _normalize_header(h) in _COMMENT_HEADER_NAMES:
            return i
    return None


def _extract_url_from_cell(cell):
    """Pull the first Reddit URL out of a single CSV cell.

    Accepts /comments/<post_id>... URLs AND /s/<token> share links.
    Cells may be a bare URL, a Google-Sheets `=HYPERLINK(...)` formula,
    a URL wrapped in quotes, or have surrounding whitespace. Also
    unwraps social-media link-tracker redirects (e.g. Instagram's
    `https://l.instagram.com/?u=<urlencoded-reddit-url>&e=...`,
    Facebook's `l.facebook.com`) — the inner Reddit URL is what the
    operator actually wants checked. Returns the URL with its query
    string stripped and trailing slash removed, or None if nothing
    matches.
    """
    if not cell:
        return None
    s = str(cell)
    # Unwrap social-media tracker redirects. They carry the real URL
    # inside a `u=` query param (urlencoded). The regex below won't
    # find a Reddit URL in the wrapper itself because the host is
    # l.instagram.com / l.facebook.com / etc., not reddit.com.
    if "l.instagram.com" in s or "l.facebook.com" in s or "lm.facebook.com" in s:
        from urllib.parse import urlparse, parse_qs, unquote
        try:
            qs = parse_qs(urlparse(s.strip()).query)
            inner = (qs.get("u") or [None])[0]
            if inner:
                inner = unquote(inner)
                if "reddit.com" in inner:
                    s = inner
        except Exception:
            pass
    m = _REDDIT_URL_RE.search(s)
    if not m:
        return None
    url = m.group(0).rstrip(".,;)")
    return url.split("?")[0].rstrip("/")


def is_share_url(url):
    """True iff the URL is a Reddit `/r/<sub>/s/<token>` share link
    that needs resolving before we can classify it. Share links don't
    contain a post_id or comment_id directly — Reddit redirects them
    to the canonical /comments/... URL.
    """
    return bool(url and _REDDIT_SHARE_RE.match(url))


def _find_comment_id_column(headers):
    """Return the 0-indexed column matching a Comment-ID-like header,
    or None when the sheet doesn't carry an explicit ID column.
    """
    for i, h in enumerate(headers or []):
        if _normalize_header(h) in _COMMENT_ID_HEADER_NAMES:
            return i
    return None


def extract_reddit_rows(csv_text: str) -> list[dict]:
    """Same shape as `extract_reddit_urls` but returns rows as dicts
    so callers (Check Live) can preserve the operator's own comment
    IDs from the sheet rather than rederiving them from the URL.

    Returns: [{"url": str | None, "comment_id": str | None,
               "skip_reason": str | None}, ...]
    **NOT** deduped — one entry per non-empty sheet row so the
    operator's expected count matches the sheet length. Rows that
    don't contain a Reddit URL still produce an entry with
    `url=None` + a `skip_reason` so the caller (Check Live) can
    emit a result row explaining why ('Comment Link cell is empty',
    'Comment Link is a non-Reddit URL', etc.) — that way the
    Check Live total matches the sheet's row count instead of
    silently dropping rows. Raises ValueError when the Comment Link
    column is missing entirely.
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
    url_idx = _find_comment_column(headers)
    if url_idx is None:
        raise ValueError(
            "Sheet must have a column named 'Comment Link' (or 'Comment URL'). "
            "Header row read: " + ", ".join(repr(h) for h in headers[:8])
            + ("..." if len(headers) > 8 else "")
        )
    id_idx = _find_comment_id_column(headers)
    out = []
    for row in reader:
        # Skip a row that's truly empty (no cells at all). Anything
        # with cells goes into the result — even if the Comment Link
        # column itself is blank, we surface it as a skip_reason so
        # the operator can see exactly which rows didn't get checked.
        if not row or not any((c or "").strip() for c in row):
            continue
        sheet_cid = None
        if id_idx is not None and id_idx < len(row):
            sheet_cid = (row[id_idx] or "").strip() or None
        # No URL cell at all (too few columns).
        if url_idx >= len(row):
            out.append({"url": None, "comment_id": sheet_cid,
                        "skip_reason": "Row has fewer columns than expected — Comment Link cell missing"})
            continue
        raw = (row[url_idx] or "").strip()
        url = _extract_url_from_cell(raw)
        if not url:
            reason = ("Comment Link cell is empty" if not raw
                      else f"Comment Link is not a Reddit URL: {raw[:80]}")
            out.append({"url": None, "comment_id": sheet_cid,
                        "skip_reason": reason})
            continue
        out.append({"url": url, "comment_id": sheet_cid, "skip_reason": None})
    return out


def extract_reddit_urls(csv_text: str) -> list[str]:
    """Pull Reddit URLs out of a Google-Sheets CSV export.

    The sheet must have a header row with a column named "Comment Link"
    (case-insensitive; "comment url" / "comment-link" / "comment_link"
    are also accepted). Other columns — including any "Post Link" —
    are deliberately ignored: the user's intent for bulk deploy is to
    flip the COMMENT rows, not the posts.

    Returns a deduped list of URLs preserving sheet order. Both full
    /comments/<post_id>... URLs AND /s/<token> share links are kept;
    the caller is expected to resolve share links before classifying.

    Raises ValueError when the Comment Link column is missing — better
    than silently scanning the whole sheet and picking up unrelated
    URLs.
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
    col_idx = _find_comment_column(headers)
    if col_idx is None:
        raise ValueError(
            "Sheet must have a column named 'Comment Link' (or 'Comment URL'). "
            "Header row read: " + ", ".join(repr(h) for h in headers[:8])
            + ("..." if len(headers) > 8 else "")
        )
    found = []
    seen = set()
    for row in reader:
        if not row or col_idx >= len(row):
            continue
        # Only the Comment Link column. Anything else in the row
        # (Post Link / notes / brand context) is intentionally ignored.
        url = _extract_url_from_cell(row[col_idx])
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
          "author": str | None,
        }

    `is_removed` is True when the body is `"[removed]"` / `"[deleted]"`
    (mod removal or user deletion). `found` is True when the target
    comment ID was located in the JSON response at all; False on
    fetch error, URL-parse failure, or 404. `author` is Reddit's
    `data.author` field — used by the matcher to narrow candidates
    by account before falling back to body similarity (much higher
    confidence than body match alone when the user posts the same
    bot-account that the row was assigned to).

    `reddit_get` is the app's `_reddit_get` (proxy + retry).
    """
    from urllib.parse import urlparse
    out = {"body": None, "posted_at": None, "is_removed": False,
           "found": False, "author": None}
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
        """Yield (id, body, created_utc, author) for every comment in the tree."""
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
                    d.get("author", "") or "",
                )
                replies = d.get("replies")
                if isinstance(replies, dict):
                    yield from _walk(replies)
        elif isinstance(node, list):
            for el in node:
                yield from _walk(el)

    for cid, body, created_utc, author in _walk(data):
        if cid == target_cid:
            out["body"] = body
            out["posted_at"] = _utc_seconds_to_iso(created_utc)
            out["found"] = True
            out["author"] = (author or "").strip() or None
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

# Statuses that count as "already finished" — bulk-deploy must not
# overwrite these. 'report' was missing before, which let a sheet
# re-upload silently roll a reported comment back to 'deployed'
# (deploy_comment hard-sets status='deployed' without looking at the
# prior state). Same risk applies to 'paid' / 'archived' / 'deleted':
# the user explicitly moved them out of the deploy pipeline; bulk
# matching shouldn't undo that.
_DONE_STATUSES = {"deployed", "paid", "archived", "report", "deleted"}


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


def _process_tier1_row(db, source, row, url, meta, posted_at, liveness):
    """Decide what to do with a single matched row (from `comments` or
    `search_comments`). Returns a per-row result dict carrying source,
    id, action, and (when applicable) reason / posted_at.

    Branches mirror the original Tier-1 logic, but on a SINGLE row so
    the caller can apply it independently to legacy + search matches.
    """
    current_status = (row.get("status") if isinstance(row, dict) else None) or ""
    rid = row["id"]

    # Already terminal in our DB — no-op.
    if current_status in _DONE_STATUSES:
        return {
            "source": source, "id": rid,
            "action": "already_deployed",
            "current_status": current_status,
        }

    # Row already 'removed' in DB (e.g. by Check Live). Leave it exactly
    # as-is — bulk deploy never writes a 'removed' state — and just report
    # that nothing changed.
    if current_status == "removed":
        return {
            "source": source, "id": rid,
            "action": "already_removed",
        }

    # A Tier-1 match means the sheet URL maps to THIS DB comment by URL /
    # comment-id — strong evidence the human actually posted it. So we deploy
    # unless Reddit POSITIVELY confirms it's gone:
    #   - "removed"  -> leave assigned (Check Live owns removed-detection).
    #   - "missing"  -> fetch failed / unreachable (the common cloud-IP 403 case)
    #                   — this is NOT evidence of removal, so it must NOT block a
    #                   URL-matched deploy. Deploy it; if it's truly gone, Check
    #                   Live flips it to removed later.
    #   - "live"     -> deploy.
    if liveness == "removed":
        return {
            "source": source, "id": rid,
            "action": "left_assigned",
            "current_status": current_status, "liveness": liveness,
        }

    # liveness is "live" or "missing" — deploy (the URL match is the evidence).
    try:
        if source == "comment":
            db.deploy_comment(rid, url, posted_at=posted_at)
        else:
            db.deploy_search_comment(rid, url, posted_at=posted_at)
    except Exception as e:
        return {
            "source": source, "id": rid,
            "action": "error", "reason": str(e),
        }
    _log_bulk_event(db, rid, source, url,
                    "bulk_deployed", current_status, "deployed")
    return {
        "source": source, "id": rid,
        "action": "deployed",
    }


# Rollup priority — when a URL touched rows in both tables with
# different outcomes, this picks the most informative action to
# surface as the "primary" report row. The other rows are listed
# under `extras` so the user still sees the full picture.
_TIER1_PRIORITY = [
    "error",
    "deployed",
    # A URL-matched row we left untouched (not live → not deployed, and
    # bulk deploy never marks removed). More informative than the
    # idempotent "already_*" states, so rank it above them.
    "left_assigned",
    "already_removed",
    "already_deployed",
]


def _rollup_tier1(url, per_row, liveness, posted_at):
    """Roll N per-row results (1 or 2 — one per matching DB table) into
    a single report dict for the bulk-deploy modal. The primary action
    is the most-informative one (deploy > mark_removed > already_*);
    additional rows are included as `extras` so the user can see if
    multiple DB rows were updated for the same URL.
    """
    if not per_row:
        return {
            "url": url, "kind": "comment", "action": "no_match",
            "reason": "tier1_internal_error",
        }
    ranked = sorted(
        per_row,
        key=lambda r: _TIER1_PRIORITY.index(r["action"]) if r["action"] in _TIER1_PRIORITY else 99,
    )
    primary = ranked[0]
    extras = ranked[1:] if len(ranked) > 1 else []
    out = {
        "url": url, "kind": "comment", "tier": "url_match",
        "source": primary["source"], "id": primary["id"],
        "action": primary["action"],
        "liveness": liveness,
    }
    if posted_at:
        out["posted_at"] = posted_at
    if primary.get("reason"):
        out["reason"] = primary["reason"]
    if extras:
        out["extras"] = [
            {"source": r["source"], "id": r["id"], "action": r["action"]}
            for r in extras
        ]
    return out


def match_and_deploy_comment(db, classified: dict, *,
                              reddit_get: Callable,
                              similarity_threshold: float = 0.5,
                              source_filter: Optional[str] = None,
                              comment_meta_fetcher: Optional[Callable] = None) -> dict:
    """Process a single comment URL — Tier 1 → Tier 2 → no-match.

    Returns a result dict the orchestrator records in tasks.progress.
    Possible action values:
      - "deployed"            (URL/body match, Reddit-confirmed live — flipped to deployed)
      - "left_assigned"       (matched, but not confirmed live (removed/unverifiable) —
                               left untouched; bulk deploy NEVER marks a comment removed)
      - "already_deployed"    (idempotent — was already deployed)
      - "already_removed"     (idempotent — was already removed)
      - "no_match"            (URL didn't map to any DB row)
      - "error"               (transient failure — see "reason")

    Adds `liveness` ("live" | "removed" | "missing") whenever we did
    fetch the Reddit JSON.
    """
    url = classified["url"]
    # `source_filter` lets the caller scope the matcher to a single
    # pipeline. The Bulk Deploy modal sets it based on the calling
    # view (Live Search → "search_comment", Live Subs → "comment")
    # so URLs in the sheet only match rows in the user's actual
    # pipeline — never the OTHER pipeline's spurious copies that
    # might exist from prior workflows.
    want_legacy = source_filter in (None, "comment")
    want_search = source_filter in (None, "search_comment")

    def _get_meta(u):
        """Fetch comment meta, preferring the RSS fetcher (cloud-IP-safe).
        Falls back to the JSON `reddit_get` path only when RSS is
        inconclusive (found=False) — covers local dev where JSON works.
        """
        if comment_meta_fetcher is not None:
            try:
                m = comment_meta_fetcher(u)
            except Exception:
                m = None
            if m and m.get("found"):
                return m
            jm = fetch_reddit_comment_meta(u, reddit_get=reddit_get)
            if jm and jm.get("found"):
                return jm
            return m or jm or {"body": None, "posted_at": None,
                               "is_removed": False, "found": False, "author": None}
        return fetch_reddit_comment_meta(u, reddit_get=reddit_get)

    # --- Tier 1: direct URL match (filtered by scope) ---------------------
    # Two passes: first an EXACT match on the stored URL, then a
    # comment-ID substring match. The substring pass catches rows where
    # the stored URL has a different slug, trailing slash, or query
    # string than the sheet URL — Reddit comment IDs are globally
    # unique, so substring-matching them is safe and high-confidence.
    tier1_matches = []  # list of (source, row)
    seen_ids = set()    # (source, id) — dedupe across the two passes
    if want_legacy:
        legacy_row = db.find_comment_by_url(url)
        if legacy_row:
            tier1_matches.append(("comment", legacy_row))
            seen_ids.add(("comment", legacy_row["id"]))
    if want_search:
        search_row = db.find_search_comment_by_url(url)
        if search_row:
            tier1_matches.append(("search_comment", search_row))
            seen_ids.add(("search_comment", search_row["id"]))
    # Tier 1.5 — by comment_id. Only fires when we have one (full URL
    # or resolved /s/ link both yield one).
    comment_id = classified.get("comment_id")
    if comment_id:
        if want_legacy:
            r = db.find_comment_by_reddit_comment_id(comment_id)
            if r and ("comment", r["id"]) not in seen_ids:
                tier1_matches.append(("comment", r))
                seen_ids.add(("comment", r["id"]))
        if want_search:
            r = db.find_search_comment_by_reddit_comment_id(comment_id)
            if r and ("search_comment", r["id"]) not in seen_ids:
                tier1_matches.append(("search_comment", r))
                seen_ids.add(("search_comment", r["id"]))

    # Shared state across Tiers 1 + 2 — both tiers may need the Reddit
    # JSON, and Tier-2 needs to know which DB rows Tier-1 already
    # touched so it doesn't process the same row twice.
    meta = None
    posted_at = None
    liveness = "missing"
    tier1_results = []
    if tier1_matches:
        meta = _get_meta(url)
        posted_at = meta.get("posted_at")
        liveness = classify_liveness(meta)
        tier1_results = [
            _process_tier1_row(db, src, row, url, meta, posted_at, liveness)
            for src, row in tier1_matches
        ]
        # Short-circuit ONLY when Tier-1 produced an actionable update.
        # If every Tier-1 match was terminal (already_deployed /
        # already_removed), the user's *intended* row is most likely
        # the one in the OTHER table that we haven't touched yet —
        # e.g. they had a legacy `comments` row that previously got
        # deployed, but their actual `search_comments` row (which they
        # see in Live Search) is still 'assigned' / 'informed' with no
        # URL attached. Falling through to Tier-2 lets the body matcher
        # find it. Errors also short-circuit so we don't double-process.
        _ACTIONABLE = {"deployed", "left_assigned", "error"}
        if any(r["action"] in _ACTIONABLE for r in tier1_results):
            return _rollup_tier1(url, tier1_results, liveness, posted_at)
        # Else: all Tier-1 matches were terminal. Continue to Tier-2.

    # --- Tier 2: post + body fuzzy match ----------------------------------
    # Match the parent post by its Reddit post ID (the immutable
    # `/comments/<id>/` segment) rather than by the full URL. URLs in
    # the user's sheet often use Reddit's `/comment/` placeholder slug
    # while our DB stores the actual title slug. Match by post-ID
    # substring.
    #
    # We now also consider posts in BOTH the legacy `posts` table AND
    # `search_posts` — a Reddit post may have been picked up by both
    # pipelines, in which case its comments live in both
    # `comments` and `search_comments`. The previous "first table
    # wins" behaviour silently hid the user's actual rows whenever a
    # legacy `posts` shadow existed.
    matching_posts = db.find_posts_by_reddit_post_id(classified["post_id"])
    if not matching_posts:
        # Tier-1 may have produced terminal results — surface them
        # rather than reporting a misleading "post not found".
        if tier1_results:
            return _rollup_tier1(url, tier1_results, liveness, posted_at)
        return {
            "url": url, "kind": "comment", "action": "no_match",
            "reason": (
                "parent post (post_id=" + str(classified["post_id"])
                + ") not found in posts or search_posts"
            ),
        }
    # Gather candidate (undeployed) comments across every matching post.
    # Each entry is augmented with its `kind` so the deploy step picks
    # the right table to update. Exclude rows Tier-1 already touched so
    # we don't re-process the same legacy already-deployed row.
    # Apply the same scope filter as Tier-1 — if the caller wants
    # search-only, don't consider legacy `comments` candidates and vice
    # versa.
    already_touched = {(r["source"], r["id"]) for r in tier1_results}
    candidates = []
    for post_kind, post_row in matching_posts:
        cand_kind = "comment" if post_kind == "post" else "search_comment"
        if cand_kind == "comment" and not want_legacy:
            continue
        if cand_kind == "search_comment" and not want_search:
            continue
        for c in db.find_undeployed_comments_for_post(post_row["id"], cand_kind):
            if (cand_kind, c["id"]) in already_touched:
                continue
            d = dict(c)
            d["__kind"] = cand_kind
            candidates.append(d)
    if not candidates:
        if tier1_results:
            return _rollup_tier1(url, tier1_results, liveness, posted_at)
        return {
            "url": url, "kind": "comment", "action": "no_match",
            "reason": "no undeployed comments under that post",
        }

    # One JSON fetch gives us body (for matching), posted_at (for the
    # column) AND liveness — no extra round trip. Reuse Tier-1's fetch
    # if we already did it.
    if meta is None:
        meta = _get_meta(url)
        posted_at = meta.get("posted_at")
        liveness = classify_liveness(meta)
    body = meta.get("body")

    # Helper: turn Tier-1 results into the `extras` list format the
    # modal renders inline next to the Tier-2 primary action.
    def _tier1_as_extras():
        return [
            {"source": r["source"], "id": r["id"], "action": r["action"]}
            for r in tier1_results
        ]

    if liveness == "missing" or body is None:
        if tier1_results:
            return _rollup_tier1(url, tier1_results, liveness, posted_at)
        # Fetch failed (cloud-IP 403 etc.), so we can't body-match. But the sheet
        # URL resolved to a real comment under a post we DO have, and if there's
        # exactly ONE undeployed candidate under that post, the URL is almost
        # certainly that comment -> deploy it (the URL is the evidence). Only
        # genuine ambiguity (2+ candidates) stays no_match.
        if len(candidates) == 1:
            cand = candidates[0]
            ck = cand["__kind"]
            try:
                if ck == "comment":
                    db.deploy_comment(cand["id"], url, posted_at=posted_at)
                else:
                    db.deploy_search_comment(cand["id"], url, posted_at=posted_at)
                _log_bulk_event(db, cand["id"], ck, url, "bulk_deployed",
                                cand.get("status"), "deployed")
                return {
                    "url": url, "source": ck, "id": cand["id"],
                    "action": "deployed", "tier": "post_singleton",
                    "liveness": liveness,
                }
            except Exception as e:
                return {"url": url, "kind": "comment", "action": "error", "reason": str(e)}
        return {
            "url": url, "kind": "comment", "action": "no_match",
            "reason": "reddit_fetch_failed_or_comment_unreachable",
            "liveness": liveness,
        }

    if liveness == "removed":
        # Comment is removed/deleted on Reddit. Bulk deploy NEVER marks a
        # comment removed — that's Check Live's job. Leave the row(s)
        # untouched in their current state and just report it.
        if tier1_results:
            return _rollup_tier1(url, tier1_results, liveness, posted_at)
        return {
            "url": url, "kind": "comment", "action": "left_assigned",
            "reason": "comment removed on reddit — left untouched (not marked removed)",
            "liveness": liveness,
        }

    # liveness == "live" — body fuzzy match. Candidates may span both
    # `comments` and `search_comments`; we pick the single best by
    # Jaccard regardless of source, then use the candidate's `__kind`
    # to route to the correct deploy_* call.
    #
    # AUTHOR NARROWING: Reddit's JSON tells us who actually posted the
    # comment. Each search_comments row has an account_id (the Reddit
    # username it was assigned to). When they match, body similarity
    # gets a much lower bar — same Reddit post AND same author is a
    # very high-confidence signal that this IS the right row, even if
    # the user edited the body when posting. Without author narrowing,
    # users who edit their drafts before posting lose body similarity
    # and the row gets reported as no_match.
    reddit_author = (meta.get("author") or "").strip().lower()
    by_author = []
    if reddit_author:
        by_author = [
            c for c in candidates
            if ((c.get("account_id") or "").strip().lower() == reddit_author)
        ]
    # If exactly one candidate has the right author, claim it
    # immediately — far more reliable than body similarity.
    if len(by_author) == 1:
        best = by_author[0]
        best_score = jaccard(body, best["body"])
        scored = [(best_score, best)]
        runner_up = 0.0
        ambiguous = False
        effective_threshold = 0.0  # author-singleton is gated by author, not body
    else:
        # Either no author info, or multiple candidates share the
        # author. In the multi-author case we narrow body match to
        # those candidates first; otherwise fall back to all candidates.
        pool = by_author if by_author else candidates
        scored = sorted(
            ((jaccard(body, c["body"]), c) for c in pool),
            key=lambda p: -p[0],
        )
        best_score, best = scored[0]
        # Threshold relaxed from 0.5 → 0.3. Bot-generated drafts often
        # get lightly edited before the user posts on Reddit; a 0.5 cut
        # was rejecting too many rows that were clearly the same
        # comment with minor edits. 0.3 still excludes random matches.
        effective_threshold = similarity_threshold if similarity_threshold < 0.5 else 0.3
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        ambiguous = runner_up >= effective_threshold and (best_score - runner_up) < 0.1
    if best_score < effective_threshold:
        # Below threshold — surface Tier-1 terminal results if we had
        # any, otherwise report the body-match miss.
        if tier1_results:
            return _rollup_tier1(url, tier1_results, liveness, posted_at)
        return {
            "url": url, "kind": "comment", "action": "no_match",
            "reason": f"best body similarity {best_score:.2f} below threshold {effective_threshold:.2f}"
                      + (f" (author={reddit_author} but no candidate matched)" if reddit_author and not by_author else ""),
            "best_id": best["id"], "best_score": round(best_score, 3),
            "liveness": liveness,
        }
    prev_status = best.get("status") if isinstance(best, dict) else None
    best_kind = best.get("__kind", "comment")
    try:
        if best_kind == "comment":
            db.deploy_comment(best["id"], url, posted_at=posted_at)
        else:
            db.deploy_search_comment(best["id"], url, posted_at=posted_at)
    except Exception as e:
        return {
            "url": url, "kind": "comment", "tier": "body_match",
            "source": best_kind, "id": best["id"],
            "action": "error", "reason": str(e),
            "liveness": liveness,
        }
    # Audit trail — one row per bulk-deploy event. Lets the user verify
    # via Activity Log that the write actually happened, independent of
    # any UI cache.
    _log_bulk_event(
        db, best["id"], best_kind, url,
        "bulk_deployed", prev_status or "", "deployed",
    )
    # Tier label: author_singleton when the row was claimed purely
    # because Reddit's author matched a single candidate's account_id
    # (the body similarity didn't gate the decision). Helps the user
    # spot author-driven matches in the report vs body-driven ones.
    tier_label = "author_singleton" if (reddit_author and len(by_author) == 1) else "body_match"
    out = {
        "url": url, "kind": "comment", "tier": tier_label,
        "source": best_kind, "id": best["id"], "action": "deployed",
        "similarity": round(best_score, 3),
        "posted_at": posted_at, "liveness": liveness,
    }
    if reddit_author:
        out["author"] = reddit_author
    if tier1_results:
        out["extras"] = _tier1_as_extras()
    if ambiguous:
        out["ambiguous"] = True
        out["runner_up_similarity"] = round(runner_up, 3)
    return out


def match_and_deploy_post(db, classified: dict, *,
                           source_filter: Optional[str] = None) -> dict:
    """Process a single post URL — match by Reddit post id so slug
    differences (e.g. user copied the URL with a different title slug
    than the one we have stored) don't cause a miss.

    `source_filter` maps the comment-side scope to the post-side: if
    the user is operating on Live Search, only `search_posts` are
    considered (kind="search_post"). The mapping is
    "comment" → "post", "search_comment" → "search_post".
    """
    url = classified["url"]
    want_legacy_post = source_filter in (None, "comment")
    want_search_post = source_filter in (None, "search_comment")
    matching = []
    for kind, row in db.find_posts_by_reddit_post_id(classified["post_id"]):
        if kind == "post" and not want_legacy_post:
            continue
        if kind == "search_post" and not want_search_post:
            continue
        matching.append((kind, row))
    if not matching:
        return {
            "url": url, "kind": "post", "action": "no_match",
            "reason": "post URL not found in posts or search_posts (in scope)",
        }
    # Prefer a non-deployed row; otherwise report already_deployed.
    target = None
    for kind, row in matching:
        if row.get("status") != "deployed":
            target = (kind, row)
            break
    if target is None:
        kind, row = matching[0]
        return {
            "url": url, "kind": "post", "source": kind,
            "id": row["id"], "action": "already_deployed",
        }
    post_kind, post_row = target
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

def resolve_reddit_share_url(short_url, *, reddit_get=None):
    """Resolve a Reddit `/r/<sub>/s/<token>` share URL to its canonical
    `/r/<sub>/comments/<post_id>/<slug>/<comment_id>` form.

    Reddit's share dialog produces /s/ links that just redirect to the
    canonical URL when you follow them. We do a HEAD with redirect-
    follow to get the final URL, then strip the query string.

    Returns the canonical URL on success, None on any failure (caller
    treats that as a no_match for the row).
    """
    try:
        import requests as _requests
    except Exception:
        return None
    if not short_url or "/s/" not in short_url:
        return short_url
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    # Use HEAD first — Reddit returns 302 → canonical. Fall back to GET
    # if HEAD doesn't redirect (some CDNs only redirect on GET).
    try:
        r = _requests.head(short_url, headers=headers,
                           allow_redirects=True, timeout=20)
        final = r.url.split("?")[0].rstrip("/") if r.url else ""
        if "/comments/" in final:
            return final
    except Exception:
        pass
    try:
        r = _requests.get(short_url, headers=headers,
                          allow_redirects=True, timeout=20)
        final = r.url.split("?")[0].rstrip("/") if r.url else ""
        if "/comments/" in final:
            return final
    except Exception:
        pass
    return None


def run_bulk_deploy(sheet_url: str, *, db_factory: Callable,
                     reddit_get: Callable,
                     source_filter: Optional[str] = None,
                     resolve_share: Optional[Callable] = None,
                     comment_meta_fetcher: Optional[Callable] = None,
                     _task_id: Optional[str] = None) -> dict:
    """Walk every URL in the sheet, match each to a DB row, mark deployed.

    `source_filter` (optional) scopes the matcher to a single pipeline:
      - "comment"        → only `comments` / `post_urls`
      - "search_comment" → only `search_comments` / `search_posts`
      - None             → both (legacy behaviour)

    Set by the Bulk Deploy modal based on the calling view (Live
    Search vs Live Subs). Prevents URLs from accidentally matching
    spurious rows in the wrong pipeline.

    `resolve_share` (optional) is a callable that takes a Reddit
    `/s/<token>` short URL and returns its canonical `/comments/...`
    form. If not provided, we use the local `resolve_reddit_share_url`
    fallback (direct request, no proxy).

    `comment_meta_fetcher` (optional) is a callable
    `(comment_url) -> {body, posted_at, is_removed, found, author}` that
    fetches a comment's metadata via Reddit RSS (the cloud-IP-safe path).
    When provided, it's preferred over the JSON `reddit_get` fetch —
    the JSON API is auth-gated for datacenter IPs, which made every row
    fall through to liveness='missing' / no_match on the deployed host.
    The JSON fetch remains as a local-dev fallback when RSS is
    inconclusive.

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
    resolver = resolve_share or resolve_reddit_share_url

    report = {
        "total": len(urls),
        "processed": 0,
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_filter": source_filter or "both",
        "results": [],
    }

    # Open a dedicated DB connection for the task thread.
    db = db_factory()
    try:
        for original_url in urls:
            # Step 1: resolve /s/ share URLs to their canonical form.
            # This is the path Reddit's "Share" button produces; without
            # resolution the URL has no post_id / comment_id and nothing
            # downstream can match it.
            url = original_url
            resolved = False
            if is_share_url(url):
                canonical = resolver(url)
                if canonical:
                    url = canonical
                    resolved = True
                else:
                    report["results"].append({
                        "url": original_url, "kind": "comment",
                        "action": "no_match",
                        "reason": "could not resolve /s/ share link to a canonical URL",
                    })
                    report["processed"] += 1
                    if _task_id:
                        try:
                            db.update_task_progress(_task_id, json.dumps(report))
                        except Exception:
                            pass
                    continue

            classified = classify_reddit_url(url)
            if not classified:
                row = {
                    "url": original_url, "action": "no_match",
                    "reason": "URL didn't parse as a Reddit post/comment",
                }
            elif classified["kind"] == "comment":
                row = match_and_deploy_comment(
                    db, classified, reddit_get=reddit_get,
                    source_filter=source_filter,
                    comment_meta_fetcher=comment_meta_fetcher,
                )
                # Preserve the original sheet URL in the report so the
                # user can correlate report rows back to their sheet.
                if resolved:
                    row["resolved_from"] = original_url
            else:
                row = match_and_deploy_post(
                    db, classified, source_filter=source_filter,
                )
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
        # Recent-removal sub-bucket: comments removed within the
        # 14-day post-publish window. Roll up under Removed for top-
        # line counts but expose the breakdown so the user knows how
        # many are eligible for redeploy.
        "marked_replace": 0,
        "already_deployed": 0,
        "already_removed": 0,
        # Matched a row but it wasn't Reddit-confirmed-live, so it was left
        # untouched in its current state (bulk deploy never marks removed).
        "left_assigned": 0,
        "no_match": 0,
        "error": 0,
        "by_tier": {"url_match": 0, "body_match": 0, "author_singleton": 0, "removed_singleton": 0},
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
