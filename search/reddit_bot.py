"""
Reddit Search Bot (Multi-API with Fallbacks)
Search Reddit for posts based on keywords and filter by various criteria.
No API credentials required!

Uses multiple APIs with automatic fallback:
1. Reddit Native JSON API (most reliable, real-time)
2. Pullpush.io (historical data, timestamp-paginated)
3. Arctic-Shift (backup)

Improvements over v1:
- Retry with exponential backoff on rate limits
- Multi-subreddit search (r/sub1+sub2+sub3 syntax)
- Excluded subreddits filter
- NSFW content control
- Minimum upvote ratio filter
- Timestamp-based pagination for Pullpush (get 1000+ results)
- Boolean query builder (AND / OR / NOT / phrases)
- Concurrent multi-keyword search
- over_18 field captured in results
"""

import os
import re
import html as _html
from collections import Counter
import requests
import xml.etree.ElementTree as _ET
from datetime import datetime, timedelta
import json
import csv
import argparse
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


def balance_posts_by_subreddit(posts, limit, subreddits, max_per_sub=None):
    """Cut a sorted list of posts down to `limit`, spread evenly across the
    requested subreddits so no single busy sub dominates the TOP of the list —
    while still RETURNING UP TO `limit` posts whenever enough exist.

    Two rounds:
      1. Even distribution — take up to `ceil(limit / N)` from each sub
         (preserving input order, which is assumed already sorted by
         score / comments / date / relevance). This is what prevents one
         sub from monopolising the head of the results.
      2. Fill — any leftover slots up to `limit` are filled from the
         top-scoring overflow across all subs.

    `max_per_sub` (default None = no hard cap): optional ceiling on how many
    posts ANY single sub may contribute (applied in BOTH rounds). Leave None
    so the requested `limit` is actually reached; pass an int only when the
    caller explicitly wants to constrain a busy sub. Historically this
    defaulted to 5, which capped multi-sub searches far below the requested
    limit (e.g. 3 subs × 5 = 15 even when 130+ posts were available) — hence
    the default is now None.

    Single-sub / global (subreddits None or length 1) returns
    `posts[:limit]` unchanged — you targeted one sub on purpose.
    """
    # Single-sub / global searches: no per-sub spreading (the user targeted
    # one sub, or none, so balancing doesn't apply).
    if not subreddits or len(subreddits) <= 1:
        return posts[:limit]

    n = len(subreddits)
    # ceil(limit / n): even target high enough that round 1 alone can reach
    # `limit` (integer floor would systematically under-fill, e.g. 50//3=16 →
    # only 48). Trimmed back to `limit` at the end.
    per_sub = max(1, -(-limit // n))
    if max_per_sub is not None:
        per_sub = min(per_sub, max_per_sub)

    # Deterministic rank: highest score first, ties broken by post id — so the
    # output depends ONLY on the input SET, never on the (concurrent, jittered)
    # order the posts were collected in. This is what stops the result count /
    # selection from drifting run-to-run.
    def _rank(p):
        return (-(p.get("score") or 0), str(p.get("id") or ""))

    by_sub = {}
    for p in posts:
        key = (p.get("subreddit") or "").lower()
        by_sub.setdefault(key, []).append(p)
    for sub in by_sub:
        by_sub[sub].sort(key=_rank)

    result = []
    leftover = []
    taken = {}
    for sub in sorted(by_sub):          # fixed sub order, not input-appearance order
        head = by_sub[sub][:per_sub]
        result.extend(head)
        taken[sub] = len(head)
        leftover.extend(by_sub[sub][per_sub:])

    # Round 2 — fill remaining slots from top-scoring overflow, honoring the
    # optional hard cap (no cap by default, so we fill all the way to `limit`).
    remaining = limit - len(result)
    if remaining > 0 and leftover:
        leftover.sort(key=_rank)
        cap = max_per_sub if max_per_sub is not None else float("inf")
        for p in leftover:
            if remaining <= 0:
                break
            sub = (p.get("subreddit") or "").lower()
            if taken.get(sub, 0) >= cap:
                continue  # this sub already hit its cap — skip
            result.append(p)
            taken[sub] = taken.get(sub, 0) + 1
            remaining -= 1
    # Round 1 with ceil can slightly overshoot (ceil(limit/n) * n ≥ limit);
    # trim back to the requested limit.
    return result[:limit]


class RedditSearchBot:
    def __init__(self, retry_attempts=3, retry_delay=2, reddit_base=None,
                 script_user_agent=None):
        """Initialize the Reddit bot with multiple API endpoints.

        `script_user_agent` (optional) — pass through the project's
        script-style UA (e.g. `REDDIT_USER_AGENT` from config). Reddit
        treats well-formed script UAs more gently than browser-mimicking
        UAs, so the default is a script-style string.

        `retry_attempts` / `retry_delay` defaults were bumped because
        the proxy now sometimes returns an HTML challenge / 403 / 5xx
        on the first try and recovers on retry.
        """
        self.apis = {
            "reddit": reddit_base or "https://www.reddit.com",
            "pullpush": "https://api.pullpush.io/reddit/search/submission",
            "arctic": "https://arctic-shift.photon-reddit.com/api/posts/search",
            "brave": "https://api.search.brave.com/res/v1/web/search",
        }
        # Optional Brave Search key — enables the `site:reddit.com` discovery
        # fallback leg. No key → the leg is skipped entirely.
        self.brave_key = os.environ.get("BRAVE_API_KEY", "")
        # Remember whether a proxy is in use — drives the
        # old.reddit.com fallback in _make_request.
        self.using_proxy = bool(reddit_base)
        # FU57: optional distinct-IP RESIDENTIAL proxy (IPRoyal gateway URL). Used ONLY as a
        # last-resort fallback when the normal Reddit path is blocked/throttled — Reddit rarely
        # blocks residential IPs, so this recovers the fetch. Bandwidth is metered, so it is NEVER
        # the default egress: the free cached/worker path runs first, this fires only on failure.
        _rp = os.environ.get("REDDIT_HTTP_PROXY", "").strip()
        self._reddit_proxies = {"http": _rp, "https": _rp} if _rp else None
        # The residential proxy is a LOW-VOLUME block fallback (check-live / comment fetch, in app.py +
        # comment_gen). In the HIGH-FAN-OUT Live SEARCH legs it is OFF by default: on a blocked cloud IP
        # the "block fallback" fires on EVERY subreddit, and metered residential latency turns a fast
        # Arctic/Pullpush search into a slow crawl. HOWEVER, on Railway's blocked cloud IP the residential
        # egress is the ONLY leg that returns search results (the free RSS/Arctic/Pullpush legs are walled),
        # so when a proxy IS configured we default this ON — and keep it fast via a capped residential
        # timeout + a wider RSS fan-out (below). Force OFF with REDDIT_SEARCH_USE_RESIDENTIAL=0/false.
        _sflag = os.environ.get("REDDIT_SEARCH_USE_RESIDENTIAL", "").strip().lower()
        self._search_use_residential = _sflag not in ("0", "false", "no", "off")
        # FU94: metered-GB visibility — bytes fetched through the residential proxy, accumulated
        # across a search (incl. its recursive reddit-wide top-up) and logged once per outer search.
        self._resi_bytes = 0
        if self._reddit_proxies:
            _sr = "ON" if self._search_use_residential else "OFF (search uses Arctic/Pullpush; "\
                  "residential still covers check-live + comments)"
            print(f"    ✓ RedditSearchBot: residential proxy available; search-leg residential = {_sr}.",
                  flush=True)
        ua = script_user_agent or os.environ.get("REDDIT_USER_AGENT") \
            or "python:reddit-strategy:v1 (search bot)"
        self.headers = {
            "User-Agent": ua,
            "Accept": "application/json",
        }
        self.retry_attempts = retry_attempts
        self.retry_delay = retry_delay
        self.current_api = None
        self._lock = threading.Lock()
        # Per-instance "Reddit is hard-blocking us" sticky flag. Once
        # _make_request exhausts every fallback (all UAs + both hosts
        # return 403 / HTML), set this and short-circuit subsequent
        # Reddit-leg calls. Critical for multi-keyword × multi-sub
        # searches: without it, every (keyword, sub) pair burns
        # ~30-60s on a doomed Reddit retry chain before the cascade
        # can move on to Pullpush. With it, the first ~30s is the
        # only Reddit cost.
        # `_reddit_dead` gates the legacy JSON API path
        # (_search_reddit_native + _make_request fallbacks). That path
        # is blocked for cloud egress IPs, so the REDDIT_API_DISABLED
        # env var pre-latches it to skip the ~30s discovery cost.
        self._reddit_dead = False
        self._reddit_dead_reason = None
        # `_reddit_rss_dead` is SEPARATE — it gates the RSS / Atom
        # path (_search_reddit_rss), which is the actual working
        # Reddit data source. RSS is NOT blocked the way JSON is, so
        # it must keep working even when the operator sets
        # REDDIT_API_DISABLED to silence the dead JSON leg. Only a
        # real 403 from an RSS endpoint latches this.
        self._reddit_rss_dead = False
        self._reddit_rss_dead_reason = None
        # Guard against the tight-window fast path recursing into its
        # own cascade fallback (which could otherwise re-trigger the
        # fast path → infinite loop).
        self._tight_window_recursion_guard = False
        # Optional opt-out: REDDIT_API_DISABLED=1 pre-latches the JSON
        # leg dead (it's blocked anyway). It deliberately does NOT
        # touch _reddit_rss_dead — RSS is the bypass and should stay
        # live. To also kill RSS (rarely needed), set
        # REDDIT_RSS_DISABLED=1.
        if os.environ.get("REDDIT_API_DISABLED", "").strip().lower() in ("1", "true", "yes"):
            self._reddit_dead = True
            self._reddit_dead_reason = "REDDIT_API_DISABLED env var is set"
        if os.environ.get("REDDIT_RSS_DISABLED", "").strip().lower() in ("1", "true", "yes"):
            self._reddit_rss_dead = True
            self._reddit_rss_dead_reason = "REDDIT_RSS_DISABLED env var is set"
        # Visibility: on a cloud host (Railway sets RAILWAY_* / PORT) Reddit
        # blocks the datacenter IP, so without a proxy the Reddit (JSON + RSS)
        # legs silently fall through to Pullpush/Arctic only. Log it once so
        # that state isn't invisible.
        if not self.using_proxy and (os.environ.get("RAILWAY_ENVIRONMENT")
                                     or os.environ.get("RAILWAY_PROJECT_ID")):
            print("    ⚠ RedditSearchBot: no REDDIT_PROXY_URL on a cloud host — "
                  "Reddit's IP wall will block direct JSON/RSS; results will rely "
                  "on Pullpush/Arctic fallbacks. Set REDDIT_PROXY_URL to restore RSS.",
                  flush=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # UA variants tried in order during the fallback rotation. Reddit
    # tightens bot-detection per-UA periodically; rotating gives us a
    # decent chance of finding one that still serves JSON. The first
    # is the script-style UA the bot was constructed with; the next
    # two are alternative shapes Reddit has historically been more
    # lenient with (mod-tool/script formats).
    _FALLBACK_UAS = [
        "python:reddit-strategy:v1 (search bot)",
        "RedditBot/1.0 (compatible)",
        "Mozilla/5.0 (compatible; redditbot/1.0)",
    ]

    def _make_request(self, url, params, timeout=10):
        """Hardened Reddit GET. Handles three failure modes:

        1. Transient HTTP errors (429/403/500/502/503/504) — retry
           with exponential backoff.
        2. HTML body where we expected JSON (Reddit's bot-challenge
           wall) — retry, then fall through to fallback chain.
        3. Persistent block on www.reddit.com — try EVERY UA in
           `_FALLBACK_UAS` against `old.reddit.com` (which Reddit
           serves more leniently). Fallback runs whether or not a
           proxy was configured — the previous version only tried
           old.reddit.com when using_proxy=True, which left direct
           callers stuck on 0 results when www.reddit.com was hard-
           walling their UA.

        Returns the final Response. Raises only when every attempt
        plus every fallback failed with a connection-level error.
        """
        last_error = None
        last_response = None
        for attempt in range(self.retry_attempts):
            try:
                response = requests.get(
                    url, params=params, headers=self.headers, timeout=timeout
                )
                # Transient HTTP errors — backoff + retry.
                if response.status_code in (429, 403, 500, 502, 503, 504):
                    if attempt < self.retry_attempts - 1:
                        # Honor Retry-After when the server sends it; else
                        # exponential backoff. Jitter so concurrent retries
                        # (multi-sub / multi-keyword) don't re-collide.
                        try:
                            ra = float(response.headers.get("Retry-After") or 0)
                        except (TypeError, ValueError):
                            ra = 0
                        wait = ra if ra > 0 else self.retry_delay * (2 ** attempt)
                        # Cap the honored Retry-After so a large server value can't
                        # stall a whole search for tens of seconds.
                        wait = min(wait, 5.0) + random.uniform(0, 1.0)
                        print(f"\n    ⏳ Reddit returned {response.status_code}. Retrying in {wait:.1f}s...", end=" ", flush=True)
                        time.sleep(wait)
                        last_response = response
                        continue
                # HTML where we expected JSON — bot-challenge page.
                if response.status_code == 200 and response.text.lstrip()[:1] == "<":
                    if attempt < self.retry_attempts - 1:
                        wait = self.retry_delay * (2 ** attempt)
                        print(f"\n    ⚠ Got HTML instead of JSON (Content-Type: {response.headers.get('Content-Type','')}). Retrying in {wait}s...", end=" ", flush=True)
                        time.sleep(wait)
                        last_response = response
                        continue
                response.raise_for_status()
                return response
            except requests.exceptions.RequestException as e:
                last_error = e
                if attempt < self.retry_attempts - 1:
                    time.sleep(self.retry_delay * (2 ** attempt))
                    continue
                break

        # ------------------------------------------------------------------
        # Fallback chain — Reddit-only. Pullpush / Arctic don't share
        # paths with reddit.com so blindly retrying their URLs against
        # old.reddit.com produced nonsense 404s and burned ~10s per
        # retry per UA per host. Only run the fallback when the
        # original URL was a Reddit host AND the bot hasn't already
        # latched _reddit_dead (in which case fallbacks are pointless).
        # ------------------------------------------------------------------
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url)
            host_lc = (parsed.netloc or "").lower()
            path = parsed.path
        except Exception:
            host_lc, path = "", None

        # Is this a Reddit URL? Match reddit.com as a DOMAIN (host == reddit.com or a *.reddit.com
        # subdomain) AND the configured proxy (if any) — the proxy is acting as Reddit so its responses
        # share Reddit's failure modes. NOTE: a naive `"reddit.com" in host_lc` SUBSTRING check misfires
        # on `arctic-shift.photon-reddit.com` (Arctic-Shift's host contains "reddit.com"!) — that mangled
        # every Arctic request onto old.reddit.com/api/posts/search (404/503/429), killed the Arctic leg,
        # and burned the whole time budget + residential GB on doomed retries.
        is_reddit_url = (
            host_lc == "reddit.com" or host_lc.endswith(".reddit.com")
            or (self.using_proxy and self.apis["reddit"] in (url or ""))
        )

        if path and is_reddit_url and not self._reddit_dead:
            for host in ("https://old.reddit.com", "https://www.reddit.com"):
                # Skip the host we just exhausted with the default UA.
                same_host_default_ua = (
                    host in (url or "") and self.headers.get("User-Agent") in self._FALLBACK_UAS[:1]
                )
                for ua in self._FALLBACK_UAS:
                    if same_host_default_ua and ua == self._FALLBACK_UAS[0]:
                        continue
                    fb_url = f"{host}{path}"
                    try:
                        print(f"\n    🔁 Fallback: {host}{path} as UA={ua[:40]!r}...", end=" ", flush=True)
                        fb_resp = requests.get(
                            fb_url, params=params,
                            headers={**self.headers, "User-Agent": ua},
                            timeout=timeout,
                        )
                        if (fb_resp.status_code == 200
                                and fb_resp.text.lstrip()[:1] != "<"):
                            print("✓", end=" ", flush=True)
                            return fb_resp
                        print(f"(status={fb_resp.status_code}, html={fb_resp.text.lstrip()[:1] == '<'})",
                              end=" ", flush=True)
                        if last_response is None or last_response.status_code != 200:
                            last_response = fb_resp
                    except Exception as fb_err:
                        print(f"(error: {fb_err})", end=" ", flush=True)
                        continue

        # FU57: last resort — retry DIRECT to old.reddit.com through the residential proxy (distinct
        # IP). Runs only after the normal path + UA/host rotation all failed, so metered GB is spent
        # only on a real block.
        if path and is_reddit_url and self._reddit_proxies and self._search_use_residential:
            try:
                pr = requests.get("https://old.reddit.com" + path, params=params,
                                  headers=self.headers, timeout=min(timeout, 15), proxies=self._reddit_proxies)
                if pr.status_code == 200 and pr.text.lstrip()[:1] != "<":
                    print("\n    ✓ Reddit JSON via residential proxy", flush=True)
                    return pr
                if last_response is None or last_response.status_code != 200:
                    last_response = pr
            except Exception as _pe:
                print(f"\n    ⚠ residential-proxy JSON retry failed: {_pe}", flush=True)

        # Mark the Reddit leg as dead so the rest of this bot's
        # lifetime skips it. Only flip when the failure looks
        # IP/host-wide (403/HTML on a Reddit URL — Pullpush/Arctic
        # failures don't say anything about Reddit's state).
        if (is_reddit_url
                and last_response is not None
                and last_response.status_code in (403, 429)
                and last_response.text.lstrip()[:1] == "<"):
            with self._lock:
                if not self._reddit_dead:
                    self._reddit_dead = True
                    self._reddit_dead_reason = (
                        f"www.reddit.com + old.reddit.com both returned "
                        f"{last_response.status_code} HTML across "
                        f"{len(self._FALLBACK_UAS)} UA variants"
                    )
                    print(f"\n    🚫 Marking Reddit leg dead for this bot: "
                          f"{self._reddit_dead_reason}", flush=True)

        if last_response is not None:
            return last_response
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"_make_request: no response for {url}")

    def _parse_post(self, p):
        """Normalise a raw post dict (Reddit native, Pullpush, or Arctic) into a
        standardised result dict."""
        ts = p.get("created_utc", 0) or 0
        try:
            post_date = datetime.utcfromtimestamp(ts)
        except (OSError, OverflowError, ValueError):
            post_date = datetime.utcnow()

        return {
            "title": p.get("title", ""),
            "url": f"https://reddit.com{p.get('permalink', '')}",
            "score": p.get("score", 0) or 0,
            "comments": p.get("num_comments", 0) or 0,
            "subreddit": p.get("subreddit", ""),
            "author": p.get("author", "[deleted]"),
            "date": post_date.strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp": ts,
            "text": (p.get("selftext", "") or "")[:500],
            "is_video": p.get("is_video", False),
            "upvote_ratio": p.get("upvote_ratio", 0) or 0,
            "domain": p.get("domain", ""),
            "external_url": p.get("url", ""),
            "flair": p.get("link_flair_text", ""),
            "id": p.get("id", ""),
            "over_18": p.get("over_18", False),
        }

    # ------------------------------------------------------------------
    # API-specific search methods
    # ------------------------------------------------------------------

    def _search_reddit_native(self, keyword, subreddit_path, sort, time_filter, limit):
        """Reddit JSON API with cursor-based pagination (up to ~1 000 results)."""
        # Hard short-circuit when this bot already discovered Reddit
        # is blocking it. Saves the per-call ~30-60s retry chain that
        # would otherwise be paid by every keyword × subreddit
        # combination in a multi-search run.
        if self._reddit_dead:
            return []
        url = (
            f"{self.apis['reddit']}/r/{subreddit_path}/search.json"
            if subreddit_path
            else f"{self.apis['reddit']}/search.json"
        )

        results = []
        after = None
        seen_ids = set()
        page = 1

        while len(results) < limit:
            batch_size = min(100, limit - len(results))
            params = {
                "q": keyword,
                "sort": sort,
                "t": time_filter,
                "limit": batch_size,
                "restrict_sr": "on" if subreddit_path else "off",
            }
            if after:
                params["after"] = after

            response = self._make_request(url, params)
            # Guard against the silent-zero-results failure mode: if
            # the body isn't JSON (HTML challenge, empty body), log
            # the response shape so future failures are diagnosable
            # from stdout instead of vanishing into "0 results".
            try:
                data = response.json()
            except Exception as json_err:
                ct = response.headers.get("Content-Type", "?")
                preview = (response.text or "")[:200].replace("\n", " ")
                print(
                    f"\n    ❌ Reddit search JSON decode failed "
                    f"(status={response.status_code}, ct={ct}): {json_err} | "
                    f"body[:200]={preview!r}",
                    flush=True,
                )
                break
            posts = data.get("data", {}).get("children", [])
            after = data.get("data", {}).get("after")

            if not posts:
                # First-page empty is the user-visible silent-zero
                # path — log enough context to triage (status,
                # Content-Type, body preview, and whether `after`
                # was set so we know if Reddit just paginated us off
                # the end on a deep query vs. truly returned nothing).
                if page == 1:
                    ct = response.headers.get("Content-Type", "?")
                    preview = (response.text or "")[:200].replace("\n", " ")
                    print(
                        f"\n    ⚠ Reddit search returned 0 results on page 1 "
                        f"(status={response.status_code}, ct={ct}, after={after!r}) "
                        f"url={url} q={params.get('q')!r} sub={subreddit_path or '(global)'} | "
                        f"body[:200]={preview!r}",
                        flush=True,
                    )
                break

            new_count = 0
            for post in posts:
                p = post.get("data", {})
                pid = p.get("id", "")
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    results.append(self._parse_post(p))
                    new_count += 1

            if not after or new_count == 0:
                break

            print(f"(page {page}: {len(results)} posts)", end=" ", flush=True)
            page += 1
            time.sleep(0.3)

        return results

    # Reddit's RSS feeds (Atom XML) — the only Reddit-hosted endpoint
    # that Reddit's bot detection currently allows cloud IPs to hit.
    # Routes:
    #   /r/<sub>/search.rss?q=KEYWORD&restrict_sr=on&sort=...&t=...&limit=N
    #   /r/<sub>/new.rss?limit=N
    #   /search.rss?q=KEYWORD&sort=...&t=...&limit=N         (global)
    # Returns Atom XML with <entry> nodes containing id (t3_xxx),
    # title, link, updated (ISO timestamp), author, content (HTML
    # selftext), and category (subreddit).
    #
    # NOTE: RSS does NOT include score, num_comments, upvote_ratio,
    # over_18, flair. Posts come back with those fields set to 0/
    # default. Filters that depend on them (min_score, min_comments,
    # nsfw, min_upvote_ratio) effectively become no-ops on RSS-sourced
    # posts. That's a deliberate trade-off: working Reddit data beats
    # perfectly-filtered nothing. For tight filters the user should
    # rely on Pullpush/Arctic which do carry these fields.
    _RSS_NS = {"atom": "http://www.w3.org/2005/Atom"}

    def _parse_rss_entry(self, entry):
        """Convert one Atom <entry> into the bot's post dict shape.
        Best-effort; missing fields default to 0/empty.
        """
        ns = self._RSS_NS

        def _text(el):
            return (el.text if el is not None else "") or ""

        title = _text(entry.find("atom:title", ns)).strip()
        link_el = entry.find("atom:link", ns)
        url = link_el.get("href") if link_el is not None else ""
        # Entry id is "t3_xxx" or sometimes "tag:reddit.com,...:t3_xxx".
        raw_id = _text(entry.find("atom:id", ns))
        m = re.search(r"t3_([a-z0-9]+)", raw_id, re.IGNORECASE)
        pid = m.group(1) if m else raw_id.split("/")[-1]
        author = _text(entry.find("atom:author/atom:name", ns)).strip()
        if author.startswith("/u/"):
            author = author[3:]
        cat_el = entry.find("atom:category", ns)
        subreddit = cat_el.get("term") if cat_el is not None else ""
        # `updated` is the post timestamp in ISO 8601 with offset.
        updated = _text(entry.find("atom:updated", ns)).strip()
        ts = 0
        if updated:
            try:
                # Strip the timezone portion (Reddit always emits +00:00)
                # for a plain isoformat parse.
                dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                ts = int(dt.timestamp())
            except Exception:
                ts = 0
        # Selftext lives in <content type="html"> wrapped with comment
        # markers (<!-- SC_OFF -->...<!-- SC_ON -->).
        content_el = entry.find("atom:content", ns)
        raw_html = _text(content_el)
        # Strip HTML tags and decode entities for a plain-text body
        # the keyword-match path can scan.
        body_plain = re.sub(r"<[^>]+>", " ", raw_html)
        body_plain = _html.unescape(body_plain)
        body_plain = re.sub(r"\s+", " ", body_plain).strip()
        # Normalize the post URL to canonical www.reddit.com. The feed
        # comes through the proxy (which routes to old.reddit.com), so
        # the <link href> often points at old.reddit.com or the proxy's
        # own worker domain. We only want the /r/SUB/comments/<id>/...
        # path and always serve it from https://www.reddit.com so the
        # links the user sees / saves are the standard Reddit URLs.
        permalink = ""
        if url:
            m2 = re.search(r"^https?://[^/]+(/r/[^?]+)", url)
            if m2:
                permalink = m2.group(1)
        canonical_url = (
            f"https://www.reddit.com{permalink}" if permalink
            else (url or "")
        )
        try:
            post_date_str = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
        except (OSError, OverflowError, ValueError):
            post_date_str = ""
        return {
            "title": title,
            "url": canonical_url,
            "score": 0,        # not in RSS
            "comments": 0,     # not in RSS
            "subreddit": subreddit,
            "author": author or "[deleted]",
            "date": post_date_str,
            "timestamp": ts,
            "text": body_plain[:500],
            "is_video": False,
            "upvote_ratio": 0,
            "domain": "",
            "external_url": canonical_url,
            "flair": "",
            "id": pid,
            "over_18": False,
            "_source": "reddit_rss",
        }

    def _resi_active(self):
        """FU94: True when the residential proxy is configured AND enabled for search — the
        worker-health latches (_reddit_rss_dead / the 429 cooldown) must NOT skip the RSS leg
        then, because the residential PRIMARY doesn't share the worker's blocks."""
        return bool(self._reddit_proxies and self._search_use_residential)

    def _count_resi_bytes(self, resp):
        """FU94: accumulate residential (metered) response bytes for the per-search log line."""
        try:
            with self._lock:
                self._resi_bytes += len(resp.content or b"")
        except Exception:
            pass

    def _search_reddit_rss(self, keyword, subreddit_path, sort, time_filter, limit):
        """Concurrency-gated entry for Reddit RSS (see `_rss_sem`)."""
        with RedditSearchBot._rss_sem:
            return self._search_reddit_rss_impl(keyword, subreddit_path, sort, time_filter, limit)

    def _search_reddit_rss_impl(self, keyword, subreddit_path, sort, time_filter, limit):
        """Reddit RSS search — the bypass for the IP/UA bot wall on
        the JSON endpoints. Hits /r/<sub>/search.rss when a sub is
        provided, /search.rss for global queries. Multi-sub (a+b+c)
        is supported via the `restrict_sr=on` flag.

        Returns a list of post dicts in the same shape `_search_reddit_native`
        returns. Short-circuits to [] only if `_reddit_rss_dead` —
        which is set by REDDIT_RSS_DISABLED or a real RSS 403, NOT by
        the REDDIT_API_DISABLED (JSON-leg) opt-out.
        """
        if self._reddit_rss_dead and not self._resi_active():
            return []   # FU94: the dead latch reflects the WORKER's 403 — residential bypasses it
        # Three URL shapes:
        #   - /r/<sub>/search.rss?q=KEYWORD  → keyword search within sub
        #   - /search.rss?q=KEYWORD          → global keyword search
        #   - /r/<sub>/new.rss               → chronological recent posts
        #                                       (when keyword is empty,
        #                                       for the tight-window
        #                                       fast path)
        # Route RSS through the configured proxy when present. Reddit
        # blocks datacenter IPs (Railway, etc.) for direct anonymous
        # access — including RSS — but a proxy whose egress IP isn't
        # on the block list serves RSS cleanly (proven via the
        # proxy-health probe: proxy → /r/<sub>/new.rss returns 200
        # Atom XML even when both JSON and direct-RSS return 403).
        # When no proxy is set (local dev), hit www.reddit.com direct.
        rss_base = (self.apis.get("reddit") or "https://www.reddit.com").rstrip("/")
        # If the proxy base accidentally points at an api path, fall
        # back to the canonical host so we don't build a broken URL.
        if "reddit.com" not in rss_base and "://" not in rss_base:
            rss_base = "https://www.reddit.com"
        # TIGHT WINDOW (≤7d → time_filter day/week): reddit's SEARCH index LAGS fresh posts, so
        # search.rss?q=…&t=day returns almost nothing for niche subs (measured: r/Machinists day=2 vs
        # all-time=100). Instead fetch /new.rss (chronological recent posts) and let the cascade's
        # WORD-BOUNDARY keyword-presence filter + the date filter select relevance client-side
        # (measured ~19 matches vs 2). Opt out with REDDIT_TIGHT_WINDOW_NEW=0.
        _tight = (time_filter in ("day", "week")
                  and os.environ.get("REDDIT_TIGHT_WINDOW_NEW", "1").strip().lower()
                      not in ("0", "false", "no", "off"))
        if subreddit_path and keyword and not _tight:
            url = f"{rss_base}/r/{subreddit_path}/search.rss"
            params = {
                "q": keyword,
                "restrict_sr": "on",
                "sort": sort or "new",
                "t": time_filter or "all",
                "limit": min(limit, 100),
            }
        elif subreddit_path:
            # keyword-less OR tight-window keyword search: chronological recent posts; keyword relevance
            # is applied client-side by the cascade's keyword-presence filter (which honors the window).
            # FU77: fetch Reddit's MAX (100) for the chronological /new.rss. The per-sub `limit` in a big
            # fan-out is small (e.g. 25 for 20+ subs), which for a BUSY sub only covers the last hour or
            # two — so keyword matches from earlier in the SAME day/week window were silently missed.
            # /new.rss is cheap (~2s/100) and we filter by keyword + date client-side, so grab the full
            # recent slice regardless of the small per-sub quota.
            url = f"{rss_base}/r/{subreddit_path}/new.rss"
            params = {"limit": 100}
        else:
            url = f"{rss_base}/search.rss"
            params = {
                "q": keyword,
                "sort": sort or "new",
                "t": time_filter or "all",
                "limit": min(limit, 100),
            }
        headers = {
            "User-Agent": self.headers.get("User-Agent", "python:reddit-strategy:v1 (rss)"),
            "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.5",
        }
        # Residential retry target: www.reddit.com + a BROWSER UA is the combination that actually returns
        # full search results — old.reddit.com's search.rss (and a bot UA) returns ~0-2, while www + browser
        # returns the full 100. The residential proxy's clean IP isn't rate-limited, so this is the path
        # that yields posts once the shared-IP worker gets throttled.
        _resi_base = "https://www.reddit.com"
        _resi_headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            "Accept": headers["Accept"],
        }
        _has_resi = bool(self._reddit_proxies and self._search_use_residential)
        results = []
        try:
            resp = None
            used_residential = False
            resi_attempted = False
            # FU94: residential-FIRST — the clean distinct-IP proxy is the RELIABLE leg (the shared-IP
            # worker 429s / serves stale empties intermittently, which is what made run-to-run counts
            # uneven). Metered GB is the price of consistency; usage is accounted + logged per search.
            if _has_resi:
                direct_url = _resi_base + url[len(rss_base):]
                resi_attempted = True
                try:
                    presp = requests.get(direct_url, params=params, headers=_resi_headers,
                                         timeout=12, proxies=self._reddit_proxies)
                    self._count_resi_bytes(presp)
                    if presp.status_code == 200:
                        print(f"\n    ✓ RSS via residential proxy for {direct_url}", flush=True)
                        resp = presp
                        used_residential = True
                    else:
                        print(f"\n    ⚠ RSS residential primary got {presp.status_code} — "
                              f"falling back to the worker", flush=True)
                except Exception as _pe:
                    print(f"\n    ⚠ RSS residential primary failed: {_pe} — "
                          f"falling back to the worker", flush=True)
            # Worker/direct path — the ONLY path when no residential proxy is configured, else the
            # FALLBACK when the residential attempt failed. Retry a transient 429 once with backoff;
            # only a 403 (hard block) latches `_reddit_rss_dead`.
            if resp is None or resp.status_code != 200:
                for rss_attempt in range(2):
                    resp = requests.get(url, params=params, headers=headers, timeout=20)
                    if resp.status_code == 429 and rss_attempt < 1:
                        # Honor Retry-After when Reddit sends it (capped at 5s so a big
                        # server value can't stall the whole search); else short backoff.
                        # Jitter so concurrent per-sub retries don't re-collide.
                        try:
                            wait = float(resp.headers.get("Retry-After") or 0)
                        except (TypeError, ValueError):
                            wait = 0
                        if wait <= 0:
                            wait = 2.0
                        wait = min(wait, 5.0) + random.uniform(0, 0.5)
                        print(f"\n    ⏳ RSS 429 on {url} — retry in {wait:.1f}s", flush=True)
                        time.sleep(wait)
                        continue
                    break
            if resp.status_code != 200:
                print(f"\n    ⚠ Reddit RSS returned {resp.status_code} for {url}", flush=True)
                # Only a persistent 403 means the wall expanded to RSS.
                # 429 is transient throughput — never latch dead on it, but DO start
                # a short cooldown so subsequent searches skip the slow RSS leg.
                if resp.status_code == 403:
                    with self._lock:
                        if not self._reddit_rss_dead:
                            self._reddit_rss_dead = True
                            self._reddit_rss_dead_reason = "RSS returned 403 (hard block)"
                            print(f"    🚫 Marking Reddit RSS leg dead: {self._reddit_rss_dead_reason}", flush=True)
                elif resp.status_code == 429:
                    RedditSearchBot._reddit_rss_cooldown_until = (
                        time.time() + RedditSearchBot._RSS_COOLDOWN_SECS)
                    print(f"    ⏸ RSS throttled (429) — cooling down the RSS leg for "
                          f"{RedditSearchBot._RSS_COOLDOWN_SECS}s", flush=True)
                return []
            # Parse Atom XML into `results` (dedup by id).
            def _parse_rss_into(text, out):
                try:
                    root = _ET.fromstring(text)
                except _ET.ParseError as e:
                    print(f"\n    ⚠ Reddit RSS XML parse failed: {e}", flush=True)
                    return
                seen = {p.get("id") for p in out}
                for entry in root.findall("atom:entry", self._RSS_NS):
                    try:
                        post = self._parse_rss_entry(entry)
                    except Exception:
                        continue
                    pid = post.get("id")
                    if pid and pid not in seen:
                        seen.add(pid)
                        out.append(post)
            _parse_rss_into(resp.text, results)

            # The Cloudflare worker's SHARED IP is rate-limited by Reddit; when throttled it often returns
            # an EMPTY 200 (stale/empty cache) instead of a 429 — so the non-200 fallback above never fires
            # and RSS silently returns 0 for subs that actually HAVE posts (verified: r/Machinists returns
            # 100 via a clean IP but ~0 via the worker). When the worker path yields ZERO entries and a
            # residential proxy is available, retry through the clean residential IP and re-parse. Only
            # fires on 0 results (the paid GB is spent exactly where the worker came back empty).
            if (not results and not used_residential and not resi_attempted and _has_resi
                    and rss_base != _resi_base):
                direct_url = _resi_base + url[len(rss_base):]
                try:
                    presp = requests.get(direct_url, params=params, headers=_resi_headers,
                                         timeout=12, proxies=self._reddit_proxies)
                    self._count_resi_bytes(presp)
                    if presp.status_code == 200 and presp.text.lstrip()[:5] == "<?xml":
                        _parse_rss_into(presp.text, results)
                        if results:
                            print(f"\n    ✓ RSS via residential proxy (worker returned empty) — "
                                  f"{len(results)} posts for {direct_url}", flush=True)
                except Exception as _pe:
                    print(f"\n    ⚠ RSS empty-200 residential retry failed: {_pe}", flush=True)

            print(f"(rss: {len(results)} posts)", end=" ", flush=True)
        except requests.exceptions.RequestException as e:
            print(f"\n    ⚠ Reddit RSS request failed: {e}", flush=True)
        return results

    def _search_pullpush(self, keyword, subreddit, sort_by, max_days_old, limit,
                         sort_order="desc"):
        """Concurrency-gated entry for Pullpush — bounds total concurrent
        Pullpush requests across all threads (see `_pullpush_sem`)."""
        with RedditSearchBot._pullpush_sem:
            return self._search_pullpush_impl(keyword, subreddit, sort_by,
                                              max_days_old, limit, sort_order=sort_order)

    def _search_pullpush_impl(self, keyword, subreddit, sort_by, max_days_old, limit,
                              sort_order="desc"):
        """Pullpush.io with timestamp-based pagination — can exceed 100 results."""
        results = []
        seen_ids = set()
        cursor_ts = None  # pagination cursor (before for desc, after for asc)
        page = 1

        after_ts = None
        if max_days_old:
            after_ts = int(
                (datetime.utcnow() - timedelta(days=max_days_old)).timestamp()
            )

        while len(results) < limit and page <= RedditSearchBot._MAX_PAGES:
            batch_size = min(100, limit - len(results))
            params = {
                "size": batch_size,
                "sort": sort_order,
                "sort_type": sort_by,
            }
            # Pullpush will return ALL recent posts in the sub when
            # `q` is omitted. The tight-window fast path
            # (_search_recent_then_filter) uses this to fetch by-sub
            # once and run keyword matching client-side, which dodges
            # Pullpush's search-index lag for very recent posts.
            if keyword:
                params["q"] = keyword
            if subreddit:
                params["subreddit"] = subreddit

            if sort_order == "asc":
                # Ascending: pagination cursor overrides time-window after_ts
                effective_after = cursor_ts if cursor_ts else after_ts
                if effective_after:
                    params["after"] = effective_after
            else:
                # Descending (default): time-window + backward pagination
                if after_ts:
                    params["after"] = after_ts
                if cursor_ts:
                    params["before"] = cursor_ts

            try:
                response = self._make_request(self.apis["pullpush"], params)
                data = response.json()
            except Exception as e:
                # One extra page-level retry before giving up. A transient blip
                # (429/timeout after _make_request's own retries, or a bad-JSON
                # body) shouldn't truncate the whole leg — that's what makes the
                # result pool swing run-to-run. Keep whatever we already have.
                print(f"({e.__class__.__name__}: {str(e)[:50]} — retrying page)")
                time.sleep(1.5 + random.uniform(0, 1.0))
                try:
                    response = self._make_request(self.apis["pullpush"], params)
                    data = response.json()
                except Exception as e2:
                    # Retries exhausted on this page — keep what we have and stop
                    # paginating. (No leg-wide cooldown: on cloud IPs that removed
                    # Pullpush as a fallback for the whole search and starved heavy
                    # multi-sub searches. The global semaphore already bounds load.)
                    print(f"    pullpush page retry failed ({e2.__class__.__name__}); keeping {len(results)}")
                    break

            posts = data.get("data", [])
            if not posts:
                break

            new_count = 0
            oldest_ts = None
            newest_ts = None
            for post in posts:
                pid = post.get("id", "")
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    results.append(self._parse_post(post))
                    new_count += 1
                    ts = post.get("created_utc", 0) or 0
                    if oldest_ts is None or ts < oldest_ts:
                        oldest_ts = ts
                    if newest_ts is None or ts > newest_ts:
                        newest_ts = ts

            # Stop if no new posts or the batch was smaller than requested
            if new_count == 0 or len(posts) < batch_size:
                break

            print(f"(page {page}: {len(results)} posts)", end=" ", flush=True)
            page += 1

            # Advance cursor: backward for desc, forward for asc
            cursor_ts = newest_ts if sort_order == "asc" else oldest_ts
            time.sleep(0.2)

        return results

    def _search_arctic(self, keyword, subreddit, max_days_old, limit):
        """Concurrency-gated entry for Arctic — bounds total concurrent Arctic
        requests across all threads (see `_arctic_sem`) so a big keyword×sub
        fan-out queues into safe waves instead of 429-storming."""
        if not subreddit:
            return []
        with RedditSearchBot._arctic_sem:
            return self._search_arctic_impl(keyword, subreddit, max_days_old, limit)

    def _search_arctic_impl(self, keyword, subreddit, max_days_old, limit):
        """Arctic-Shift API with timestamp-based pagination.

        NOTE: Arctic-Shift requires a subreddit filter when using text search.
        Returns empty list immediately if no subreddit is specified.
        """
        if not subreddit:
            return []  # Arctic-Shift requires a subreddit for text queries
        # Circuit-breaker: if a sibling query already saw "maintenance" / "slow down",
        # skip — don't burn the budget hammering a down/rate-limited Arctic-Shift.
        if time.time() < RedditSearchBot._arctic_cooldown_until:
            return []

        results = []
        seen_ids = set()
        before_ts = None
        pages = 0

        while len(results) < limit and pages < RedditSearchBot._MAX_PAGES:
            pages += 1
            batch_size = min(100, limit - len(results))
            params = {"query": keyword, "subreddit": subreddit, "limit": batch_size}
            if max_days_old:
                after_date = datetime.utcnow() - timedelta(days=max_days_old)
                params["after"] = after_date.strftime("%Y-%m-%d")
            if before_ts:
                params["before"] = str(int(before_ts))

            def _cool_if_down(err):
                # Trip the Arctic circuit-breaker on a maintenance / rate-limit signal so the rest of
                # the fan-out (and the next search) skips Arctic instead of burning the budget.
                if any(s in (err or "").lower()
                       for s in ("maintenance", "too many", "slow down", "rate")):
                    RedditSearchBot._arctic_cooldown_until = (
                        time.time() + RedditSearchBot._ARCTIC_COOLDOWN_SECS)
            try:
                response = self._make_request(self.apis["arctic"], params)
                data = response.json()
                # Arctic-Shift returns errors in the response body
                if data.get("error"):
                    print(f"(Arctic-Shift error: {data['error'][:50]})")
                    _cool_if_down(data["error"])
                    break
            except Exception as e:
                # One extra page-level retry before giving up (see pullpush
                # rationale): a transient blip shouldn't truncate the leg and
                # shrink the result pool. Keep whatever we already collected.
                print(f"({e.__class__.__name__}: {str(e)[:50]} — retrying page)")
                time.sleep(1.5 + random.uniform(0, 1.0))
                try:
                    response = self._make_request(self.apis["arctic"], params)
                    data = response.json()
                    if data.get("error"):
                        print(f"(Arctic-Shift error: {data['error'][:50]})")
                        _cool_if_down(data["error"])
                        break
                except Exception as e2:
                    print(f"    arctic page retry failed ({e2.__class__.__name__}); keeping {len(results)}")
                    # A page that fails even after _make_request's own retries = Arctic-Shift is
                    # down/rate-limiting → cool it down so the rest of the fan-out skips it.
                    RedditSearchBot._arctic_cooldown_until = (
                        time.time() + RedditSearchBot._ARCTIC_COOLDOWN_SECS)
                    break

            posts = data.get("data", [])
            if not posts:
                break

            new_count = 0
            oldest_ts = None
            for post in posts:
                pid = post.get("id", "")
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    results.append(self._parse_post(post))
                    new_count += 1
                    ts = post.get("created_utc", 0) or 0
                    if oldest_ts is None or (ts and ts < oldest_ts):
                        oldest_ts = ts

            if new_count == 0 or len(posts) < batch_size:
                break

            before_ts = oldest_ts
            time.sleep(0.2)

        return results

    def _search_brave(self, keyword, subreddits, limit, max_days_old=None):
        """Discovery fallback via the Brave Search API (`site:reddit.com`).

        INDEPENDENT of Reddit's rate limits — returns Reddit post URLs from Brave's
        web index. URL + title + snippet only (NO score / num_comments), so brave
        posts are tagged `_source="brave"` and treated like RSS posts by the filters.
        No key → []; any error → [] (never breaks the cascade)."""
        if not self.brave_key or not (keyword or "").strip():
            return []
        # Map the day window to Brave's coarse freshness buckets.
        freshness = None
        if max_days_old:
            if max_days_old <= 1:
                freshness = "pd"
            elif max_days_old <= 7:
                freshness = "pw"
            elif max_days_old <= 31:
                freshness = "pm"
            elif max_days_old <= 366:
                freshness = "py"
        want_subs = {str(s).strip().lower() for s in (subreddits or []) if str(s).strip()}
        headers = {"X-Subscription-Token": self.brave_key,
                   "Accept": "application/json", "Accept-Encoding": "gzip"}
        results = []
        seen = set()
        url_re = re.compile(r"reddit\.com/r/([^/]+)/comments/([a-z0-9]+)", re.IGNORECASE)
        try:
            for page in range(2):  # ≤2 pages (free tier is rate-limited)
                if len(results) >= limit:
                    break
                params = {"q": f"site:reddit.com {keyword}", "count": 20, "offset": page}
                if freshness:
                    params["freshness"] = freshness
                resp = requests.get(self.apis["brave"], params=params, headers=headers,
                                    timeout=15, proxies={"http": None, "https": None})
                if resp.status_code != 200:
                    print(f"    Brave search returned {resp.status_code}", flush=True)
                    break
                web = (resp.json() or {}).get("web") or {}
                hits = web.get("results") or []
                if not hits:
                    break
                for h in hits:
                    u = (h.get("url") or "").strip()
                    m = url_re.search(u)
                    if not m:
                        continue
                    sub, pid = m.group(1), m.group(2).lower()
                    if want_subs and sub.lower() not in want_subs:
                        continue
                    if pid in seen:
                        continue
                    seen.add(pid)
                    # Brave's page_age is an ISO date when present; best-effort epoch.
                    ts = 0
                    page_age = h.get("page_age") or h.get("age")
                    if page_age:
                        try:
                            ts = int(datetime.strptime(page_age[:10], "%Y-%m-%d").timestamp())
                        except (ValueError, TypeError):
                            ts = 0
                    results.append({
                        "id": pid, "subreddit": sub,
                        "title": (h.get("title") or "").strip(),
                        "url": f"https://www.reddit.com/r/{sub}/comments/{pid}/",
                        "text": (h.get("description") or "").strip(),
                        "score": 0, "comments": 0, "num_comments": 0,
                        "timestamp": ts, "author": "", "_source": "brave",
                    })
                if len(hits) < 20:
                    break
                time.sleep(1.1)  # free tier ~1 req/s
        except Exception as e:
            print(f"    Brave search failed: {e}", flush=True)
            return []
        print(f"(brave: {len(results)} reddit posts)", end=" ", flush=True)
        return results[:limit]

    # ------------------------------------------------------------------
    # Public search interface
    # ------------------------------------------------------------------

    def search(
        self,
        keyword,
        subreddit=None,
        subreddits=None,
        excluded_subreddits=None,
        min_comments=0,
        min_score=0,
        max_days_old=None,
        sort_by="relevance",
        sort_order="desc",
        limit=100,
        api="auto",
        nsfw=None,
        min_upvote_ratio=None,
        max_subscribers=None,
        min_subscribers=None,
        max_scrutiny=None,
        db=None,
        db_path=None,
        force_refresh=False,
        keywords=None,
        reddit_wide=False,
        max_per_sub=None,
        _deadline=None,
    ):
        # `keywords` (optional): the ORIGINAL term list when `keyword` is a
        # combined boolean-OR query. OR-capable legs (RSS/JSON) use the combined
        # `keyword` (one query per sub — collapses the N×M fan-out to M). Arctic
        # ignores boolean OR, so it fans out per-term — but bounded by a global
        # call budget so a big keyword×sub combo can't 429-storm it.
        _terms = [t for t in (keywords or []) if str(t).strip()]
        """
        Search Reddit for posts matching the given criteria.

        Args:
            keyword: Search term. Supports Reddit operators: AND, OR, NOT,
                     and quoted phrases e.g. '"low T" OR TRT NOT women'.
                     Use build_query() to construct complex queries.
            subreddit: Single subreddit to search (None = all of Reddit).
            subreddits: List of subreddits to search simultaneously,
                        e.g. ["testosterone", "trt", "malehealth"].
                        Takes precedence over `subreddit`.
            excluded_subreddits: Subreddits whose posts are removed from results,
                                  e.g. ["polyamory", "AskReddit"].
            min_comments: Minimum comment count.
            min_score: Minimum post score (upvotes).
            max_days_old: Only return posts newer than this many days.
            sort_by: "relevance" | "score" | "comments" | "new"
            sort_order: "desc" | "asc" (applied locally after fetch).
            limit: Maximum posts to return.
            api: "auto" | "reddit" | "pullpush" | "arctic"
            nsfw: None = include all, True = only NSFW, False = exclude NSFW.
            min_upvote_ratio: Float 0.0–1.0. Filter out low-quality posts.
            max_subscribers: Maximum subscriber count for a post's subreddit.
                             Posts from larger subs are filtered out.

        Returns:
            List of post dicts.
        """
        if _deadline is None:
            self._resi_bytes = 0   # FU94: fresh metered-GB tally for this outer search
        # Short-TTL result cache: an identical back-to-back search returns the
        # SAME set instead of re-rolling the rate-limited upstream legs (which
        # is what makes the count swing run-to-run, e.g. 3 then 10). Keyed by
        # the result-affecting inputs only; `force_refresh` bypasses it.
        _subs_for_key = subreddits if subreddits else ([subreddit] if subreddit else [])
        cache_sig = json.dumps({
            "kw": (keyword or "").strip().lower(),
            "subs": sorted(s.strip().lower() for s in _subs_for_key),
            "excl": sorted(s.strip().lower() for s in (excluded_subreddits or [])),
            "minc": min_comments, "mins": min_score, "days": max_days_old,
            "sort": sort_by, "order": sort_order, "limit": limit, "api": api,
            "nsfw": nsfw, "ratio": min_upvote_ratio, "maxsub": max_subscribers,
            "minsub": min_subscribers, "scrut": max_scrutiny, "wide": bool(reddit_wide),
            "mpsub": max_per_sub,
        }, sort_keys=True, default=str)
        if not force_refresh:
            with RedditSearchBot._result_cache_lock:
                hit = RedditSearchBot._result_cache.get(cache_sig)
                if hit and (time.time() - hit[0]) < RedditSearchBot._RESULT_CACHE_TTL:
                    print(f"    ↩ result cache hit ({len(hit[1])} posts) — same query within "
                          f"{RedditSearchBot._RESULT_CACHE_TTL}s", flush=True)
                    return list(hit[1])

        # Build subreddit path (Reddit supports r/a+b+c syntax)
        if subreddits:
            subreddit_path = "+".join(s.strip() for s in subreddits)
        elif subreddit:
            subreddit_path = subreddit
        else:
            subreddit_path = None

        reddit_sort_map = {
            "score": "top",
            "num_comments": "comments",
            "comments": "comments",
            "created_utc": "new",
            "new": "new",
            "relevance": "relevance",
        }
        reddit_sort = reddit_sort_map.get(sort_by, "relevance")

        time_filter = "all"
        if max_days_old:
            if max_days_old <= 1:
                time_filter = "day"
            elif max_days_old <= 7:
                time_filter = "week"
            elif max_days_old <= 30:
                time_filter = "month"
            elif max_days_old <= 365:
                time_filter = "year"
            else:
                time_filter = "all"  # >1 year: search all time, client-side date filter applies

        # ------------------------------------------------------------------
        # Unified cascade + inline filter loop
        # Counts only filter-passing posts toward `limit`, so the CSV always
        # receives `limit` results (or as many as the APIs can supply).
        # ------------------------------------------------------------------
        cutoff_date = (
            datetime.utcnow() - timedelta(days=max_days_old) if max_days_old else None
        )
        excluded_set = {s.lower() for s in (excluded_subreddits or [])}

        # Cascade order. For SUB-SCOPED searches, run Arctic BEFORE Pullpush:
        # Arctic queries the named subs directly, is fast, and fills the limit —
        # so the early-stop then skips Pullpush, which is slow and frequently
        # returns 0 for niche subs (measured: 12 dead Pullpush calls = 73s wasted
        # on a 4kw×3sub search). For GLOBAL (no-sub) searches keep Pullpush first,
        # because Arctic's global mode needs a prior leg's results to discover
        # which subs to query.
        if api == "auto":
            apis_to_try = (["reddit", "arctic", "pullpush"] if subreddit_path
                           else ["reddit", "pullpush", "arctic"])
            # Brave (site:reddit.com) is a DISCOVERY FALLBACK — last, and only when
            # a key is set. The early-stop means it runs only if the metadata-rich
            # legs underfill, so it adds no latency to the common case.
            if self.brave_key:
                apis_to_try.append("brave")
        else:
            apis_to_try = [api]
        filtered = []   # only posts that pass all filters
        seen_ids = set()

        # Detect whether strict filters are active (high rejection rate expected)
        has_strict_filters = (min_comments > 0 or min_score > 0
                              or min_upvote_ratio is not None
                              or max_subscribers is not None
                              or min_subscribers is not None
                              or nsfw is not None or excluded_set)

        # Hard wall-clock budget so a search can't grind for minutes when residential RSS underfills and
        # the slow Arctic (sub×term fan-out) / Pullpush legs + reddit-wide recursion stack up. Shared
        # across the reddit_wide recursion via _deadline. Returns whatever was gathered by the deadline.
        search_deadline = _deadline or (time.time() + self._SEARCH_TIME_BUDGET)

        for api_name in apis_to_try:
            # Stop early if we already have enough results
            if len(filtered) >= limit:
                break
            if time.time() > search_deadline:
                print(f"    ⏱ search time budget ({self._SEARCH_TIME_BUDGET:.0f}s) reached — "
                      f"returning {len(filtered)} result(s) gathered so far", flush=True)
                break

            # The 'reddit' leg now means RSS (the JSON path is dead for
            # cloud IPs). Skip it only when the RSS path itself is dead
            # — NOT when _reddit_dead is set (that only reflects the
            # JSON-leg opt-out / block, which RSS bypasses).
            if api_name == "reddit" and self._reddit_rss_dead and not self._resi_active():
                print(f"    Skipping reddit RSS API (marked dead: {self._reddit_rss_dead_reason})", flush=True)
                continue
            if (api_name == "reddit" and not self._resi_active()
                    and time.time() < RedditSearchBot._reddit_rss_cooldown_until):
                # FU94: the 429 cooldown protects the WORKER path only — with the residential
                # PRIMARY available, the RSS leg always runs (this was the run-to-run variance).
                left = int(RedditSearchBot._reddit_rss_cooldown_until - time.time())
                print(f"    Skipping reddit RSS API (recently 429'd — cooling down {left}s)", flush=True)
                continue
            if api_name == "arctic" and time.time() < RedditSearchBot._arctic_cooldown_until:
                left = int(RedditSearchBot._arctic_cooldown_until - time.time())
                print(f"    Skipping Arctic API (under maintenance / rate-limited — cooling down {left}s)", flush=True)
                continue

            # Adaptive over-fetch, scaled to the requested limit (NOT a flat 500).
            # Filters reject some raw posts, so fetch a multiple of what's still
            # needed — but a flat floor of 500 made every filtered search paginate
            # the slow Arctic/Pullpush legs to ~250-500 posts (≈20s) to return 40.
            # 3× with a limit-scaled floor keeps ~2.5× headroom for rejection while
            # cutting pagination to ~1-2 pages. Hard-capped so a huge limit can't
            # blow it up.
            multiplier = 4 if has_strict_filters else 2
            floor = max(limit * 2, 100) if has_strict_filters else max(limit, 50)
            needed_raw = min(max((limit - len(filtered)) * multiplier, floor), 250)
            try:
                print(f"    Trying {api_name} API...", end=" ", flush=True)

                if api_name == "reddit":
                    # Reddit's JSON endpoints are walled off for cloud
                    # egress IPs (Railway, Cloudflare Workers, etc.),
                    # but the RSS / Atom endpoints are still served
                    # cleanly with no UA discrimination. Switched the
                    # primary Reddit data path to RSS — gets us back
                    # current Reddit data without OAuth credentials.
                    # Trade-off: RSS doesn't include score / num_comments
                    # / upvote_ratio / over_18 — those fields come back
                    # as 0/default. Filters depending on them become
                    # no-ops on RSS-sourced posts; Pullpush + Arctic
                    # still carry those signals if the user has tight
                    # filters.
                    if subreddit_path and "+" in subreddit_path:
                        subs = [s.strip() for s in subreddit_path.split("+")
                                if s.strip()]
                        batch = []
                        per_sub = max(needed_raw // max(len(subs), 1), 25)
                        # Reddit rate-limits RSS per-IP — firing every sub at
                        # once trips 429 on ALL of them (each then returns 0).
                        # Throttle: a small worker pool + a jittered per-sub
                        # stagger so requests are spaced under the limit. RSS
                        # is fast (~2s/100 posts), so this costs little.
                        def _staggered_rss(idx, sub):
                            # Cap the per-sub stagger so a 20+ sub fan-out doesn't add ~8s of sleep to the
                            # last subs; the residential IP tolerates a wider pool than the shared IP did.
                            time.sleep(min(idx * 0.4, 2.0) + random.uniform(0, 0.2))
                            return self._search_reddit_rss(
                                keyword, sub, reddit_sort, time_filter, per_sub)
                        with ThreadPoolExecutor(max_workers=min(len(subs), 5)) as ex:
                            futures = [
                                ex.submit(_staggered_rss, i, sub)
                                for i, sub in enumerate(subs)
                            ]
                            for f in as_completed(futures):
                                try:
                                    batch.extend(f.result() or [])
                                except Exception as e:
                                    print(f"    per-sub fetch error (reddit rss): {e}")
                                if time.time() > search_deadline:
                                    for _fut in futures:
                                        _fut.cancel()
                                    break
                    elif not subreddit_path and len(_terms) >= 2:
                        # FU86 — GLOBAL multi-keyword search: fan out ONE precise query PER original
                        # term instead of a single combined-OR blob. Measured (5-term OR, t=day):
                        # 98 raw -> 87 off-keyword noise -> 9 usable; a single term ("shopify") alone
                        # returned 72 usable. Reddit's global search matches an OR blob loosely (often
                        # via comments), so the Reddit-wide top-up was precision-starved. Per-term
                        # queries mirror what the Arctic leg already does (it has no OR either).
                        g_terms = _terms[:6]   # bounded — a pathological keyword list can't 429-storm
                        per_term = max(needed_raw // max(len(g_terms), 1), 50)
                        batch = []

                        def _staggered_global(idx, term):
                            time.sleep(min(idx * 0.4, 2.0) + random.uniform(0, 0.2))
                            return self._search_reddit_rss(
                                term, None, reddit_sort, time_filter, per_term)
                        with ThreadPoolExecutor(max_workers=min(len(g_terms), 3)) as ex:
                            futures = [
                                ex.submit(_staggered_global, i, t)
                                for i, t in enumerate(g_terms)
                            ]
                            for f in as_completed(futures):
                                try:
                                    batch.extend(f.result() or [])
                                except Exception as e:
                                    print(f"    per-term fetch error (reddit rss): {e}")
                                if time.time() > search_deadline:
                                    for _fut in futures:
                                        _fut.cancel()
                                    break
                    else:
                        batch = self._search_reddit_rss(
                            keyword, subreddit_path, reddit_sort, time_filter, needed_raw
                        )
                elif api_name == "pullpush":
                    if subreddit_path and "+" in subreddit_path:
                        # Multi-sub: Pullpush only accepts a single subreddit,
                        # so split and query each with equal quota — in parallel.
                        # Cap at 5 (was 8): Pullpush 429s at ~10 concurrent,
                        # and with the outer 5-keyword pool that bounds total
                        # in-flight Pullpush requests at 5×5 = 25. Stayed at 5
                        # (not 3) because per-keyword serialization wrecks
                        # throughput on 10+ keyword queries.
                        subs = [s.strip() for s in subreddit_path.split("+")
                                if s.strip()]
                        batch = []
                        per_sub = max(needed_raw // max(len(subs), 1), 50)
                        with ThreadPoolExecutor(max_workers=min(len(subs), 5)) as ex:
                            futures = [
                                ex.submit(self._search_pullpush,
                                          keyword, sub, sort_by, max_days_old, per_sub,
                                          sort_order=sort_order)
                                for sub in subs
                            ]
                            for f in as_completed(futures):
                                try:
                                    batch.extend(f.result() or [])
                                except Exception as e:
                                    print(f"    per-sub fetch error (pullpush): {e}")
                                if time.time() > search_deadline:
                                    for _fut in futures:
                                        _fut.cancel()
                                    break
                    else:
                        batch = self._search_pullpush(
                            keyword, subreddit_path, sort_by, max_days_old,
                            needed_raw, sort_order=sort_order,
                        )
                elif api_name == "arctic":
                    # Resolve the sub list (explicit, or discovered from prior legs
                    # for a global search) and the TERM list (original terms when a
                    # combined-OR query was passed, since Arctic ignores boolean OR).
                    if subreddit_path and "+" not in subreddit_path:
                        arctic_subs = [subreddit_path]
                    elif subreddit_path:
                        arctic_subs = [s.strip() for s in subreddit_path.split("+") if s.strip()]
                    else:
                        sub_counts, disc = {}, {}
                        for post in filtered:
                            s = (post.get("subreddit") or "")
                            if s:
                                k = s.lower(); disc[k] = s; sub_counts[k] = sub_counts.get(k, 0) + 1
                        arctic_subs = [disc[k] for k in
                                       sorted(sub_counts, key=sub_counts.get, reverse=True)[:10]]
                    arctic_terms = _terms if _terms else [keyword]
                    # (sub, term) work-list, term-outer so EVERY sub is covered by the
                    # first term(s); capped at a global budget so a big keyword×sub
                    # combo can't 429-storm Arctic (it's a bounded supplement — RSS's
                    # combined-OR query already covers all terms across all subs).
                    # For a combined-OR multi-keyword search the RSS leg already
                    # covers all terms across all subs in M calls; Arctic (no OR)
                    # would fan out per-term (N×M) and is SLOW, so keep it a tight
                    # supplement. Single-keyword searches fan out per-sub as usual.
                    if _terms and len(arctic_terms) > 1:
                        # Combined-OR multi-keyword search. RSS covers all terms
                        # across all subs in M calls; Arctic (no OR) is the FALLBACK
                        # for when RSS is throttled (cloud IP / proxy 429) and returns
                        # little. The old term-outer slice queried only the FIRST
                        # keyword (term[0] × first 12 subs) -> near-0 when term[0] is
                        # sparse. Instead ROUND-ROBIN the terms across every sub (two
                        # offset passes) so the bounded budget spreads over ALL
                        # keywords. Concurrency is still capped by _arctic_sem(4), and
                        # a healthy RSS fills the limit first so the early-stop skips
                        # this entirely -- it only runs (and only costs latency) in the
                        # throttled case where we actually need the coverage.
                        # Cover the FULL sub×term grid (the per-keyword baseline's
                        # coverage), but ORDER it round-robin (term rotates per sub,
                        # one offset pass after another) so even a budget cut still
                        # spreads over ALL keywords and ALL subs. Capped at 96 so a
                        # pathological combo can't run away; concurrency is bounded by
                        # _arctic_sem(4) regardless, so a bigger budget only costs
                        # latency in the throttled case (RSS underfilled) -- a healthy
                        # RSS fills the limit first and the early-stop skips Arctic.
                        _ARCTIC_BUDGET = min(len(arctic_subs) * len(arctic_terms), 40)
                        pairs, _seen_pairs = [], set()
                        for _pass in range(len(arctic_terms)):
                            for _i, sub in enumerate(arctic_subs):
                                term = arctic_terms[(_i + _pass) % len(arctic_terms)]
                                if (sub, term) not in _seen_pairs:
                                    _seen_pairs.add((sub, term))
                                    pairs.append((sub, term))
                        pairs = pairs[:_ARCTIC_BUDGET]
                    else:
                        _ARCTIC_BUDGET = max(limit, 30)
                        pairs = [(sub, term) for term in arctic_terms
                                 for sub in arctic_subs][:_ARCTIC_BUDGET]
                    batch = []
                    if pairs:
                        per_pair = max(needed_raw // max(len(pairs), 1), 25)
                        with ThreadPoolExecutor(max_workers=min(len(pairs), 4)) as ex:
                            futures = [
                                ex.submit(self._search_arctic, term, sub, max_days_old, per_pair)
                                for (sub, term) in pairs
                            ]
                            for f in as_completed(futures):
                                try:
                                    batch.extend(f.result() or [])
                                except Exception as e:
                                    print(f"    per-pair fetch error (arctic): {e}")
                                if time.time() > search_deadline:
                                    for _fut in futures:
                                        _fut.cancel()
                                    break
                        if len(arctic_terms) > 1 or not subreddit_path:
                            print(f"(arctic {len(pairs)} sub×term queries) ", end="")
                elif api_name == "brave":
                    # One Brave query; filter to the requested subs client-side.
                    brave_subs = ([s.strip() for s in subreddit_path.split("+") if s.strip()]
                                  if subreddit_path else None)
                    batch = self._search_brave(keyword, brave_subs, needed_raw, max_days_old)
                else:
                    batch = []

                # Apply all filters inline — only count posts that pass.
                # Per-filter rejection counters so we can surface which
                # filter is killing results when new_count==0 despite
                # batch_total>0. Mirrors what's actually in the loop.
                new_count = 0
                rej = {
                    "dup_or_no_id": 0, "min_comments": 0, "min_score": 0,
                    "too_old": 0, "excluded_sub": 0, "nsfw": 0,
                    "min_upvote_ratio": 0,
                }
                for post in batch:
                    pid = post.get("id", "")
                    if not pid or pid in seen_ids:
                        rej["dup_or_no_id"] += 1
                        continue
                    # RSS-sourced posts carry no score / num_comments /
                    # upvote_ratio / over_18 (Reddit's RSS omits them →
                    # 0/False). Skip those four filters for RSS posts so
                    # a min_comments=1 default doesn't reject 100% of
                    # them. Pullpush/Arctic posts keep full filtering.
                    # RSS and Brave posts carry no score/comments/nsfw/ratio
                    # metadata — skip those filters for them (else a min_comments=1
                    # default would reject 100%). Pullpush/Arctic keep full filtering.
                    is_rss = post.get("_source") in ("reddit_rss", "brave")
                    if not is_rss:
                        if post["comments"] < min_comments:
                            rej["min_comments"] += 1
                            continue
                        if post["score"] < min_score:
                            rej["min_score"] += 1
                            continue
                    if cutoff_date and post["timestamp"]:
                        try:
                            if datetime.utcfromtimestamp(post["timestamp"]) < cutoff_date:
                                rej["too_old"] += 1
                                continue
                        except (OSError, OverflowError, ValueError):
                            pass
                    if excluded_set and post.get("subreddit", "").lower() in excluded_set:
                        rej["excluded_sub"] += 1
                        continue
                    if not is_rss:
                        if nsfw is True and not post.get("over_18"):
                            rej["nsfw"] += 1
                            continue
                        if nsfw is False and post.get("over_18"):
                            rej["nsfw"] += 1
                            continue
                        if (
                            min_upvote_ratio is not None
                            and post.get("upvote_ratio", 0) < min_upvote_ratio
                        ):
                            rej["min_upvote_ratio"] += 1
                            continue
                    # Post passed all filters
                    seen_ids.add(pid)
                    filtered.append(post)
                    new_count += 1

                # Diagnostic: show filter pass rate so the user understands
                # why fewer results may be returned with strict filters.
                batch_total = len(batch)
                if new_count:
                    pass_rate = (new_count / batch_total * 100) if batch_total else 0
                    print(f"✓ (+{new_count} passed filters out of "
                          f"{batch_total} raw, {len(filtered)} total, "
                          f"{pass_rate:.0f}% pass rate)")
                    self.current_api = api_name
                else:
                    # When 100% are filtered out, surface the per-
                    # filter rejection counts so the operator can see
                    # exactly which knob is too strict.
                    reasons = ", ".join(
                        f"{k}={v}" for k, v in rej.items() if v
                    ) or "no posts in batch"
                    print(f"No new results ({batch_total} raw, all filtered out): {reasons}")
                    # Early-termination guard: if a leg returns ≥100
                    # posts and EVERY one is rejected by the same
                    # filter, paging further is hopeless — the filter
                    # is structurally incompatible with this query.
                    # Bail out so the next API leg isn't blocked and
                    # the cascade doesn't spend minutes on doomed
                    # fetches (e.g. min_score set higher than any
                    # post in the subreddit). Threshold ≥100 avoids
                    # false-positives on small first batches.
                    if batch_total >= 100:
                        dominant = max(rej.items(), key=lambda kv: kv[1], default=(None, 0))
                        if dominant[1] == batch_total and dominant[0] != "dup_or_no_id":
                            print(f"    ⛔ Bailing on {api_name}: filter "
                                  f"`{dominant[0]}` rejected 100% of "
                                  f"{batch_total} posts. Loosen this "
                                  f"filter and retry.")
                            continue

            except Exception as e:
                print(f"✗ ({str(e)[:60]})")
                continue

            # Apply subscriber filter after each API batch so len(filtered)
            # reflects the true count and adaptive over-fetch works correctly
            if (max_subscribers or min_subscribers) and filtered:
                before_sub = len(filtered)
                filtered = self._filter_by_subscribers(
                    filtered, max_subscribers, min_subscribers=min_subscribers)
                removed = before_sub - len(filtered)
                if removed:
                    print(f"    Subscriber filter: removed {removed}, {len(filtered)} remaining")

        if not filtered:
            print("    ⚠ All APIs failed or returned no results")
            return []

        # ── Keyword-presence precision filter ─────────────────────────
        # Reddit's search.rss matches loosely — it returns posts where the
        # query term appears anywhere (often only in a COMMENT, or in a
        # body Reddit indexed but RSS truncated), so the result set
        # includes posts whose visible title/body don't contain the
        # keyword at all. Those are off-topic for brand-mention targeting
        # and get correctly skipped at gen time as low-relevance — but
        # they shouldn't be in the result set in the first place.
        #
        # Keep only posts where the keyword's most distinctive (longest)
        # token appears as a word-boundary match in the title or body.
        # Word-boundary (not substring) so "hr" matches the term "HR" but
        # not "through". Single distinctive token (not all tokens) so a
        # multi-word keyword like "physiotherapy australia" matches a post
        # titled "physiotherapy clinic?" without requiring every word.
        # Opt out with REDDIT_KW_FILTER=0.
        if ((keyword and keyword.strip()) or _terms) and \
                os.environ.get("REDDIT_KW_FILTER", "1").strip().lower() not in ("0", "false", "no"):
            import re as _kwre
            # For a combined-OR query keep a post matching ANY original term
            # (a single pivot would wrongly drop posts matching the other terms);
            # for a single keyword fall back to that keyword's most distinctive
            # token. One pivot token per term so a multi-word term like
            # "physiotherapy australia" matches "physiotherapy clinic?".
            filter_terms = _terms if _terms else [keyword]
            pats, labels = [], []
            for term in filter_terms:
                ttoks = [t.strip('"\'').lower() for t in _kwre.split(r"\s+", str(term).strip())]
                ttoks = [t for t in ttoks if t and t not in ("and", "or", "not")]
                if ttoks:
                    pivot = max(ttoks, key=len)
                    # Leading word-boundary only (no trailing \b) so plurals /
                    # suffixed forms still match: "loan" -> loans/loaned,
                    # "credit" -> credits, "installment" -> installments. The
                    # leading \b still prevents substring noise ("loan" !~ "sloan").
                    # Require a pivot of >=4 chars for the prefix form so a short
                    # token can't over-match; shorter pivots keep the strict \b..\b.
                    if len(pivot) >= 4:
                        pats.append(_kwre.compile(r"\b" + _kwre.escape(pivot), _kwre.IGNORECASE))
                    else:
                        pats.append(_kwre.compile(r"\b" + _kwre.escape(pivot) + r"\b", _kwre.IGNORECASE))
                    labels.append(pivot)
            if pats:
                before_kw = len(filtered)
                kept = [p for p in filtered
                        if any(pat.search((p.get("title", "") + " " + (p.get("text", "") or "")))
                               for pat in pats)]
                lbl = "|".join(labels[:6]) + ("…" if len(labels) > 6 else "")
                # TIGHT window uses /new.rss (NOT keyword-matched upstream) → apply this filter FULLY (drop
                # every non-match; that's the whole point of the fast path). BROAD windows use search.rss
                # (already keyword-matched, but RSS truncates the body so a pivot can be absent for a
                # genuine match) → only trim a MINORITY; if it would drop half-or-more, keep them (else a
                # thin niche search collapses to ~1). Opt out entirely with REDDIT_KW_FILTER=0.
                _tight_kw = bool(max_days_old and max_days_old <= 7)
                if _tight_kw or (kept and len(kept) >= max(1, (before_kw + 1) // 2)):
                    if before_kw - len(kept):
                        print(f"    Keyword-presence filter ('{lbl}'): dropped "
                              f"{before_kw - len(kept)} off-keyword posts, {len(kept)} remain")
                    filtered = kept
                else:
                    print(f"    Keyword-presence filter ('{lbl}'): would drop "
                          f"{before_kw - len(kept)} of {before_kw} (majority/all) — keeping unfiltered "
                          f"(legs already keyword-matched; RSS body is truncated)")

        # Always compute scrutiny scores (annotate every result);
        # only drop posts when max_scrutiny is provided.
        try:
            filtered = self._filter_by_scrutiny(
                filtered, max_scrutiny=max_scrutiny, db=db, db_path=db_path)
        except Exception as e:
            print(f"    ⚠ scrutiny pass failed: {e}")

        # Sort locally (filtering already done above)
        sort_key_map = {
            "score": "score",
            "comments": "comments",
            "num_comments": "comments",
            "new": "timestamp",
            "created_utc": "timestamp",
        }
        if sort_by in sort_key_map:
            key = sort_key_map[sort_by]
            # Deterministic tiebreaker: stable two-pass sort — first by id, then
            # by the requested key — so equal-key posts always resolve the same
            # way regardless of the order they were collected in.
            filtered.sort(key=lambda x: str(x.get("id") or ""))
            filtered.sort(key=lambda x: x.get(key, 0), reverse=(sort_order == "desc"))

        # Equal distribution across subreddits when multiple are searched.
        # FU77: `max_per_sub` (default None) caps how many any single sub may contribute so no busy
        # sub over-represents; the Reddit-wide top-up below honors the same cap on the merged result.
        result = balance_posts_by_subreddit(filtered, limit, subreddits, max_per_sub=max_per_sub)

        # Reddit-wide top-up: the named subreddits didn't have enough matching posts
        # to fill the limit → search ALL of Reddit by the same keyword(s) + filters
        # and append fresh posts until the limit is reached. The named-sub results
        # keep their priority/order; globals only fill the remaining tail. Opt-in via
        # `reddit_wide` (the Live Search endpoint turns it on). No recursion loop: the
        # inner call passes subreddits=None, so its own guard is False.
        if (reddit_wide and subreddit_path and api == "auto" and len(result) < limit
                and time.time() < search_deadline):
            seen_ids = {p.get("id") for p in result if p.get("id")}
            needed = limit - len(result)
            # FU77: enforce max_per_sub across the WHOLE result — seed per-sub counts from the
            # already-balanced named-sub result so a Reddit-wide top-up post is skipped when its sub is
            # already at the cap (else a popular sub could over-represent via the global fill).
            sub_counts = {}
            if max_per_sub is not None:
                for p in result:
                    s = (p.get("subreddit") or "").lower()
                    if s:
                        sub_counts[s] = sub_counts.get(s, 0) + 1
            try:
                global_hits = self.search(
                    keyword, subreddit=None, subreddits=None,
                    excluded_subreddits=excluded_subreddits,
                    min_comments=min_comments, min_score=min_score,
                    max_days_old=max_days_old, sort_by=sort_by, sort_order=sort_order,
                    limit=min(needed * 2, 200), api="auto", nsfw=nsfw,
                    min_upvote_ratio=min_upvote_ratio, max_subscribers=max_subscribers,
                    min_subscribers=min_subscribers, max_scrutiny=max_scrutiny,
                    db=db, db_path=db_path, force_refresh=force_refresh,
                    keywords=keywords, reddit_wide=False, _deadline=search_deadline,
                )
                added = 0
                for p in global_hits:
                    if len(result) >= limit:
                        break
                    pid = p.get("id")
                    if not pid or pid in seen_ids:
                        continue
                    s = (p.get("subreddit") or "").lower()
                    if max_per_sub is not None and s and sub_counts.get(s, 0) >= max_per_sub:
                        continue  # this sub already at the cap — don't let the global fill over-represent it
                    seen_ids.add(pid)
                    result.append(p)
                    if s:
                        sub_counts[s] = sub_counts.get(s, 0) + 1
                    added += 1
                if added:
                    print(f"    Reddit-wide top-up: named subs had {len(result) - added}, "
                          f"added {added} from all of Reddit (target {limit})")
            except Exception as e:
                print(f"    Reddit-wide top-up failed: {e}")

        # Cache the assembled result so an identical re-run is consistent (and
        # skips the upstream calls). The all-APIs-failed path returns earlier
        # WITHOUT caching, so a transient total failure isn't locked in.
        with RedditSearchBot._result_cache_lock:
            RedditSearchBot._result_cache[cache_sig] = (time.time(), list(result))
        if _deadline is None and self._resi_bytes:
            print(f"    [reddit_bot] residential egress ≈ "
                  f"{self._resi_bytes / 1_048_576:.1f} MB this search", flush=True)
        return result

    # Class-level subscriber cache (persists across searches within same process)
    _sub_cache = {}  # sub_name_lower -> (subscriber_count, timestamp)
    _SUB_CACHE_TTL = 3600  # 1 hour

    # Class-level short-TTL RESULT cache, shared across instances (each search
    # builds a fresh bot). Keyed by the result-affecting search inputs so an
    # identical back-to-back search returns the SAME set instead of re-rolling
    # the rate-limited upstream legs — fixes run-to-run count swings (e.g. 3
    # then 10) and cuts 429 load. Class-level lock because the per-instance
    # `self._lock` doesn't protect cross-instance state.
    _result_cache = {}  # signature -> (timestamp, results list)
    _RESULT_CACHE_TTL = 600  # 10 minutes
    _result_cache_lock = threading.Lock()

    # Class-level RSS cooldown (shared across instances). When the RSS leg gets
    # rate-limited (429 after retries), we set this to a near-future timestamp and
    # SKIP the RSS leg until it passes — going straight to Pullpush/Arctic, which
    # is both faster AND higher-yield when RSS is throttled (a throttled RSS burns
    # ~10s of retries and only partially fills the quota, cutting off Arctic). Auto-
    # expires so a recovered RSS rejoins; a healthy RSS never trips it. Softer,
    # auto-expiring sibling of the 403 `_reddit_rss_dead` latch.
    _reddit_rss_cooldown_until = 0.0
    _RSS_COOLDOWN_SECS = 90

    # Arctic-Shift circuit-breaker: when it's under maintenance or rate-limiting
    # ("Too many complex queries. Please slow down."), STOP hammering it — otherwise
    # its per-query 503/429 backoff-retries burn the whole search time budget for 0
    # results. Set on the first such error; the leg + queued fan-out queries skip
    # until it expires (auto-recovers).
    _arctic_cooldown_until = 0.0
    _ARCTIC_COOLDOWN_SECS = 300

    # Same idea for Pullpush: it frequently 429s / returns nothing for niche subs
    # and grinds through retries (measured ~2-11s per dead call). When a call
    # exhausts its retries on 429, cool the leg down so subsequent searches skip
    # it instead of paying that latency again. Auto-expires; healthy Pullpush
    # never trips it.
    _pullpush_cooldown_until = 0.0
    _PULLPUSH_COOLDOWN_SECS = 90

    # Hard cap on pagination pages per leg (Pullpush/Arctic) — a latency backstop
    # so a high needed_raw or a deep subreddit can never paginate the slow legs
    # indefinitely. ~3 pages × 100 = up to ~300 raw, plenty for the over-fetch.
    _MAX_PAGES = 3
    # Hard wall-clock budget for a single search() (env-overridable). Caps the worst case when the
    # residential RSS leg underfills and the slow Arctic (sub×term fan-out) / Pullpush legs + the
    # reddit-wide recursion pile up — the search returns whatever it gathered by the deadline.
    _SEARCH_TIME_BUDGET = float(os.environ.get("REDDIT_SEARCH_BUDGET", "40"))

    # GLOBAL per-host concurrency caps (shared across ALL keyword/sub threads of
    # ALL in-flight searches). The nested keyword-pool × sub-pool executors can
    # otherwise fire ~20 concurrent Arctic requests — well past Arctic's ~5
    # limit → a 429-storm that returns almost nothing. These semaphores bound
    # total concurrent requests per host so a big keyword×sub fan-out queues into
    # safe waves instead of storming.
    _arctic_sem = threading.Semaphore(4)
    _pullpush_sem = threading.Semaphore(6)
    _rss_sem = threading.Semaphore(6)  # RSS is the primary leg for combined-OR; Worker serves-stale on 429

    def _filter_by_subscribers(self, results, max_subscribers, min_subscribers=None):
        # Reddit API is the only data source for subscriber counts.
        # If we already know it's blocked, every fetch is going to
        # time out / return HTML, wasting ~8s per sub. Skip the
        # filter entirely so the cascade isn't blocked by doomed
        # network calls. Posts pass through unfiltered (sub_subscribers
        # left None on each result).
        if self._reddit_dead:
            print(f"    Subscriber filter skipped (Reddit leg dead): "
                  f"keeping {len(results)} posts unfiltered", flush=True)
            for r in results:
                r["sub_subscribers"] = None
            return results
        """Filter results to posts from subs within [min_subscribers, max_subscribers].

        Fetches subscriber counts for each unique subreddit via Reddit API.
        Uses in-memory cache to avoid redundant lookups.
        Attaches sub_subscribers to each result for frontend display.
        Posts from subs where the count can't be determined are kept.
        """
        unique_subs = list(set(r.get("subreddit", "") for r in results if r.get("subreddit")))
        max_disp = f"{max_subscribers:,}" if max_subscribers else "∞"
        min_disp = f"{min_subscribers:,}" if min_subscribers else "0"
        print(f"    Checking subscriber counts for {len(unique_subs)} subreddits (range: {min_disp}–{max_disp})...")

        # Use bot-style UA for Reddit JSON API
        headers = dict(self.headers)
        ua = headers.get("User-Agent", "")
        if "Mozilla" in ua or "AppleWebKit" in ua:
            headers["User-Agent"] = "SubredditStrategyBot/2.0 (by /u/strategy_bot_admin)"

        now = time.time()
        sub_info = {}

        # Check cache first
        to_fetch = []
        for sub_name in unique_subs:
            key = sub_name.lower()
            cached = self._sub_cache.get(key)
            if cached and (now - cached[1]) < self._SUB_CACHE_TTL:
                sub_info[key] = cached[0]
                symbol = "✓" if (cached[0] or 0) <= max_subscribers else "✗"
                print(f"      {symbol} r/{sub_name}: {cached[0]:,} (cached)")
            else:
                to_fetch.append(sub_name)

        # Fetch uncached subs using concurrent threads
        if to_fetch:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def fetch_sub(sub_name):
                try:
                    resp = requests.get(
                        f"{self.apis['reddit']}/r/{sub_name}/about.json",
                        headers=headers, timeout=8
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        subs_count = data.get("data", {}).get("subscribers", 0)
                        return sub_name, subs_count
                    else:
                        return sub_name, None
                except Exception:
                    return sub_name, None

            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {executor.submit(fetch_sub, s): s for s in to_fetch}
                for future in as_completed(futures):
                    sub_name, count = future.result()
                    key = sub_name.lower()
                    sub_info[key] = count
                    if count is not None:
                        self._sub_cache[key] = (count, now)
                    symbol = "✓" if count is not None and count <= max_subscribers else ("✗" if count is not None else "?")
                    print(f"      {symbol} r/{sub_name}: {count:,} subscribers" if count is not None else f"      ? r/{sub_name}: unknown (keeping)")

        # Attach subscriber count to each result & filter by [min, max] range
        filtered = []
        for r in results:
            count = sub_info.get(r.get("subreddit", "").lower())
            r["sub_subscribers"] = count
            if count is None:
                filtered.append(r)
                continue
            if max_subscribers is not None and count > max_subscribers:
                continue
            if min_subscribers is not None and count < min_subscribers:
                continue
            filtered.append(r)

        removed = len(results) - len(filtered)
        print(f"    Subscriber filter: kept {len(filtered)}, removed {removed}")
        return filtered

    # Class-level scrutiny cache (in-memory, backed by DB if provided)
    _scrutiny_cache = {}  # sub_name_lower -> (score_dict, timestamp)
    _SCRUTINY_CACHE_TTL = 7 * 24 * 3600  # 7 days

    def _compute_sub_scrutiny(self, sub_name, post_results, headers):
        """Compute scrutiny signals for a single subreddit using ONLY
        publicly-available unauthenticated endpoints.

        Signals (0-100, higher = stricter moderation):
          - rules_count (from /about/rules.json)  — weight 35
          - submit_text length (from /about.json) — weight 25
          - gate_penalty (submission_type/type)   — weight 20
          - comment_removal_rate (from /comments.json, best-effort) — weight 20

        Returns dict with metrics. Returns None if sub is private/quarantined.
        """
        base = self.apis["reddit"]
        info = {
            "subreddit_type": None,
            "submission_type": None,
            "rules_count": None,
            "submit_text_len": None,
            "comment_removal_rate": None,
            "post_removal_rate": None,
            "gate_penalty": 0.0,
            "scrutiny_score": None,
            "subscribers": None,
        }

        # 1) about.json — subreddit_type, submission_type, submit_text, subscribers
        try:
            resp = requests.get(f"{base}/r/{sub_name}/about.json",
                                headers=headers, timeout=8)
            if resp.status_code == 200:
                data = (resp.json() or {}).get("data", {}) or {}
                info["subreddit_type"] = data.get("subreddit_type")
                info["submission_type"] = data.get("submission_type")
                info["subscribers"] = data.get("subscribers")
                info["submit_text_len"] = len((data.get("submit_text") or ""))
                if info["subreddit_type"] in ("private", "quarantined"):
                    return None
        except Exception:
            pass

        # 2) about/rules.json — rules count
        try:
            resp = requests.get(f"{base}/r/{sub_name}/about/rules.json",
                                headers=headers, timeout=8)
            if resp.status_code == 200:
                info["rules_count"] = len((resp.json() or {}).get("rules", []) or [])
        except Exception:
            pass

        # 3) comments.json — best-effort comment removal rate (often 0 for
        # logged-out because Reddit hides removed comments from listings,
        # but still useful when it does surface them)
        try:
            resp = requests.get(f"{base}/r/{sub_name}/comments.json",
                                params={"limit": 100}, headers=headers, timeout=8)
            if resp.status_code == 200:
                children = ((resp.json() or {}).get("data", {}) or {}).get("children", []) or []
                total = len(children)
                if total:
                    removed_ct = 0
                    for c in children:
                        cd = (c or {}).get("data", {}) or {}
                        body = (cd.get("body") or "").strip()
                        author = (cd.get("author") or "").strip()
                        if body in ("[removed]", "[deleted]") or author == "[deleted]":
                            removed_ct += 1
                    info["comment_removal_rate"] = removed_ct / total
        except Exception:
            pass

        # 4) post_removal_rate from already-fetched results for this sub (free)
        if post_results:
            total_p = len(post_results)
            removed_p = 0
            for p in post_results:
                body = (p.get("text") or "").strip()
                author = (p.get("author") or "").strip()
                if body in ("[removed]", "[deleted]") or author == "[deleted]":
                    removed_p += 1
            if total_p:
                info["post_removal_rate"] = removed_p / total_p

        # 5) gate penalty (0-1)
        gate = 0.0
        if info["submission_type"] and info["submission_type"] != "any":
            gate += 0.4
        if info["subreddit_type"] in ("restricted", "gold_only"):
            gate += 0.6
        info["gate_penalty"] = min(gate, 1.0)

        # 6) Final score (0-100):
        #    rules  (35) : 0 rules → 0, 12+ rules → 35
        #    submit (25) : 0 chars → 0, 500+ chars → 25
        #    gate   (20) : 0 → 0, 1.0 → 20
        #    crr    (20) : 0 → 0, 1.0 → 20  (bonus signal when present)
        rules_n = min((info["rules_count"] or 0) / 12.0, 1.0)
        submit_n = min((info["submit_text_len"] or 0) / 500.0, 1.0)
        crr = info["comment_removal_rate"] or 0.0
        info["scrutiny_score"] = round(
            rules_n * 35 + submit_n * 25 + info["gate_penalty"] * 20 + crr * 20, 1
        )
        return info

    def _filter_by_scrutiny(self, results, max_scrutiny=None, db=None, db_path=None):
        """Annotate each result with scrutiny_score; optionally drop results
        whose sub scores above `max_scrutiny`.

        - Always computes/attaches scores (UI opt-in filtering).
        - Uses DB cache (7-day TTL) via `db.get_scrutiny`/`db.upsert_scrutiny`
          when available, plus an in-memory class cache.
        - Drops posts from private/quarantined subs unconditionally.

        Thread-safety: when called from a worker thread (e.g. via
        `search_multiple_keywords(concurrent=True)`), pass `db_path` instead
        of `db` — the method will open its own per-call SQLite connection.
        SQLite connections are single-thread only; passing a shared `db`
        instance from a different thread will raise and cache writes will fail.
        """
        if not results:
            return results

        # Reddit is the data source for scrutiny signals (rules,
        # submit_text, comment removal rate). If it's blocked, every
        # sub fetch returns HTML/403 — the function would otherwise
        # spend ~24s per sub on three timed-out requests. Skip
        # entirely so the cascade isn't blocked. Posts pass through
        # with scrutiny_score=None (no max_scrutiny filtering possible).
        if self._reddit_dead:
            print(f"    Scrutiny filter skipped (Reddit leg dead): "
                  f"keeping {len(results)} posts unfiltered", flush=True)
            for r in results:
                r.setdefault("scrutiny_score", None)
            return results

        # If we only have a db_path (thread-safe case), open a fresh connection
        # for this call and close it at the end.
        _own_db = False
        if db is None and db_path:
            try:
                from db import Database as _Database
                db = _Database(db_path)
                db.connect()
                _own_db = True
            except Exception as e:
                print(f"    ⚠ scrutiny: failed to open db_path: {e}")
                db = None

        # Group posts by subreddit (case-insensitive)
        by_sub = {}
        for r in results:
            sub = r.get("subreddit", "")
            if not sub:
                continue
            by_sub.setdefault(sub.lower(), {"name": sub, "posts": []})["posts"].append(r)

        unique_subs = list(by_sub.values())
        print(f"    Computing scrutiny for {len(unique_subs)} subreddits...")

        headers = dict(self.headers)
        ua = headers.get("User-Agent", "")
        if "Mozilla" in ua or "AppleWebKit" in ua:
            headers["User-Agent"] = "SubredditStrategyBot/2.0 (by /u/strategy_bot_admin)"

        now = time.time()
        scores = {}  # key -> info dict or None (blocked)
        to_fetch = []

        # Check in-memory cache, then DB cache
        for entry in unique_subs:
            key = entry["name"].lower()
            mem = self._scrutiny_cache.get(key)
            if mem and (now - mem[1]) < self._SCRUTINY_CACHE_TTL:
                scores[key] = mem[0]
                print(f"      r/{entry['name']}: {mem[0].get('scrutiny_score') if mem[0] else 'BLOCKED'} (mem cache)")
                continue
            if db is not None:
                try:
                    cached = db.get_scrutiny(entry["name"], max_age_days=7)
                except Exception:
                    cached = None
                if cached:
                    info = {
                        "subreddit_type": cached.get("subreddit_type"),
                        "submission_type": None,
                        "comment_removal_rate": cached.get("comment_removal_rate"),
                        "post_removal_rate": cached.get("post_removal_rate"),
                        "gate_penalty": cached.get("gate_penalty") or 0.0,
                        "scrutiny_score": cached.get("scrutiny_score"),
                        "subscribers": cached.get("subscribers"),
                    }
                    if info["subreddit_type"] in ("private", "quarantined"):
                        scores[key] = None
                    else:
                        scores[key] = info
                    self._scrutiny_cache[key] = (scores[key], now)
                    print(f"      r/{entry['name']}: {info.get('scrutiny_score')} (db cached)")
                    continue
            to_fetch.append(entry)

        # Fetch uncached subs concurrently
        if to_fetch:
            def fetch_one(entry):
                name = entry["name"]
                try:
                    info = self._compute_sub_scrutiny(name, entry["posts"], headers)
                    return name, info
                except Exception:
                    return name, {"scrutiny_score": 50.0, "gate_penalty": 0.0,
                                  "comment_removal_rate": None, "post_removal_rate": None,
                                  "subreddit_type": None, "subscribers": None}

            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {executor.submit(fetch_one, e): e for e in to_fetch}
                for future in as_completed(futures):
                    name, info = future.result()
                    key = name.lower()
                    scores[key] = info
                    self._scrutiny_cache[key] = (info, now)
                    if info is None:
                        print(f"      r/{name}: BLOCKED (private/quarantined)")
                    else:
                        print(f"      r/{name}: {info.get('scrutiny_score')} "
                              f"(crr={info.get('comment_removal_rate')}, "
                              f"gate={info.get('gate_penalty')})")
                    # Persist to DB
                    if db is not None and info is not None:
                        try:
                            db.upsert_scrutiny(
                                name,
                                subscribers=info.get("subscribers"),
                                comment_removal_rate=info.get("comment_removal_rate"),
                                post_removal_rate=info.get("post_removal_rate"),
                                gate_penalty=info.get("gate_penalty"),
                                scrutiny_score=info.get("scrutiny_score"),
                                subreddit_type=info.get("subreddit_type"),
                            )
                        except Exception as e:
                            print(f"      ⚠ failed to cache r/{name}: {e}")

        # Annotate results & filter
        filtered = []
        for r in results:
            sub = r.get("subreddit", "")
            key = sub.lower()
            info = scores.get(key)
            if info is None and key in scores:
                # Hard-exclude private/quarantined
                continue
            if info is not None:
                r["scrutiny_score"] = info.get("scrutiny_score")
                r["scrutiny_comment_removal_rate"] = info.get("comment_removal_rate")
            else:
                r["scrutiny_score"] = None
            if (max_scrutiny is not None
                    and r.get("scrutiny_score") is not None
                    and r["scrutiny_score"] > max_scrutiny):
                continue
            filtered.append(r)

        removed = len(results) - len(filtered)
        if max_scrutiny is not None:
            print(f"    Scrutiny filter (max={max_scrutiny}): kept {len(filtered)}, removed {removed}")
        else:
            print(f"    Scrutiny computed for {len(filtered)} posts (no filter)")

        # Close per-call thread-local connection if we opened it
        if _own_db and db is not None:
            try:
                db.close()
            except Exception:
                pass

        return filtered

    def _search_recent_then_filter(self, keywords, subreddits, **kwargs):
        """Tight-window optimization: for `max_days_old` ≤ 7 across N
        subs × K keywords, the standard per-(kw, sub) Pullpush fan-out
        is wasteful — most (kw, sub, 24h) buckets are empty even when
        recent posts in those subs would match the keyword set.

        This path fetches the last `max_days_old` of posts per sub
        ONCE (no keyword filter at Pullpush), then runs a fast Python
        substring match over title + body for any of the keywords.
        Behaviorally matches a `(kw1 OR kw2 OR ... OR kwN)` query the
        Pullpush API can't directly express.

        Args:
            keywords: list[str] — at least one must substring-match
                a post's title or body for it to be kept. Case-insensitive.
            subreddits: list[str] — one Pullpush call per sub.
            **kwargs: search() kwargs. Honoured:
                max_days_old, limit, min_comments, min_score,
                excluded_subreddits, nsfw, min_upvote_ratio.
                The rest (max_subscribers, max_scrutiny) are skipped
                because they require Reddit-API fetches.

        Returns:
            list of post dicts with .matched_keywords annotated.
        """
        max_days_old = kwargs.get("max_days_old") or 1
        limit = kwargs.get("limit", 50)
        min_comments = kwargs.get("min_comments", 0) or 0
        min_score = kwargs.get("min_score", 0) or 0
        excluded_set = {s.lower() for s in (kwargs.get("excluded_subreddits") or [])}
        nsfw = kwargs.get("nsfw")
        min_upvote_ratio = kwargs.get("min_upvote_ratio")
        # Each sub gets a generous per-sub budget (200) since we're
        # only making 1 call per sub — fast even on slow days.
        per_sub_budget = 200

        # Build a WORD-BOUNDARY regex per keyword. A naive substring
        # test (`kw in text`) made short keywords like "hr" match
        # "through" / "chrome" / "Chris" → flooded results with junk.
        # `\bkw\b` matches "hr"/"HR" as a token but not those words.
        # Strip quotes / boolean operators the user may have pasted.
        kw_patterns = []  # list of (label, compiled_regex)
        for kw in keywords:
            cleaned = (kw or "").strip().lower()
            for tok in ('"', "'", " AND ", " OR ", " NOT "):
                cleaned = cleaned.replace(tok.lower(), " ")
            cleaned = " ".join(cleaned.split())  # collapse whitespace
            if not cleaned:
                continue
            try:
                pat = re.compile(r"\b" + re.escape(cleaned) + r"\b", re.IGNORECASE)
            except re.error:
                pat = None
            kw_patterns.append((cleaned, pat))
        if not kw_patterns:
            return []

        # Per-sub fetch, concurrent. Cap at 5 to stay under Pullpush
        # rate limit — even with 20 subs this finishes in 4 batches
        # × ~5s = ~20s.
        all_posts = []
        seen_ids = set()
        sort_by = kwargs.get("sort_by", "relevance")
        # For tight-window mode, Pullpush sort_type should be
        # created_utc (newest first) so we get the most recent posts
        # within the budget.
        pp_sort_type = "created_utc"
        # Pick the data source. Reddit RSS (/r/<sub>/new.rss) is the
        # best option when reachable — real-time recent posts with no
        # rate limit and no search-index lag. Falls back to Pullpush
        # only when the RSS path is dead (REDDIT_RSS_DISABLED or a real
        # RSS 403). Note: this checks _reddit_rss_dead, NOT _reddit_dead
        # — the JSON-leg opt-out must not disable RSS.
        use_rss = (not self._reddit_rss_dead) or self._resi_active()   # FU94
        # Map period of `max_days_old` to a Reddit `t` window. RSS
        # also accepts `t=day/week/month/year/all` for the listings
        # path but not for /new (which is purely chronological).
        if max_days_old <= 1:
            time_filter = "day"
        elif max_days_old <= 7:
            time_filter = "week"
        else:
            time_filter = "all"

        def _fetch_recent(sub):
            try:
                if use_rss:
                    # /r/<sub>/new.rss returns chronological recent
                    # posts. We get up to 100 per call. No keyword,
                    # no time filter — we enforce the time window
                    # client-side via the timestamp check below.
                    return self._search_reddit_rss(
                        keyword="", subreddit_path=sub, sort="new",
                        time_filter=time_filter, limit=per_sub_budget,
                    )
                # Fallback to Pullpush when Reddit RSS is dead.
                return self._search_pullpush(
                    keyword="", subreddit=sub, sort_by=pp_sort_type,
                    max_days_old=max_days_old, limit=per_sub_budget,
                    sort_order="desc",
                )
            except Exception as e:
                print(f"    per-sub recent fetch error ({sub}): {e}")
                return []

        with ThreadPoolExecutor(max_workers=min(len(subreddits), 5)) as ex:
            futures = {ex.submit(_fetch_recent, sub): sub for sub in subreddits}
            for f in as_completed(futures):
                try:
                    for p in (f.result() or []):
                        pid = p.get("id")
                        if pid and pid not in seen_ids:
                            seen_ids.add(pid)
                            all_posts.append(p)
                except Exception as e:
                    print(f"    fetch result error: {e}")

        print(f"    Tight-window fetched {len(all_posts)} unique recent posts "
              f"across {len(subreddits)} subs.")

        # Client-side filter: keyword match + standard filters.
        cutoff_ts = (datetime.utcnow() - timedelta(days=max_days_old)).timestamp()
        matched = []
        rej = {"no_kw_match": 0, "too_old": 0, "min_comments": 0,
               "min_score": 0, "excluded_sub": 0, "nsfw": 0,
               "min_upvote_ratio": 0}
        for p in all_posts:
            text = (p.get("title", "") + " " + p.get("text", ""))
            hit_kw = None
            for label, pat in kw_patterns:
                if pat is not None:
                    if pat.search(text):
                        hit_kw = label
                        break
                elif label in text.lower():  # fallback if regex compile failed
                    hit_kw = label
                    break
            if not hit_kw:
                rej["no_kw_match"] += 1
                continue
            ts = p.get("timestamp", 0)
            if ts and ts < cutoff_ts:
                rej["too_old"] += 1
                continue
            # CRITICAL: RSS-sourced posts have no score / num_comments /
            # upvote_ratio / over_18 (Reddit's RSS feed omits them, so
            # they come back as 0/False). Applying min_comments / min_score
            # / nsfw / min_upvote_ratio to them would reject 100%
            # unconditionally — a min_comments=1 default silently kills
            # every RSS result. So skip those four filters for RSS posts;
            # they're no-ops when the data isn't present. Posts from
            # Pullpush/Arctic (which DO carry the fields) are still
            # filtered normally.
            is_rss = p.get("_source") == "reddit_rss"
            if not is_rss:
                if p.get("comments", 0) < min_comments:
                    rej["min_comments"] += 1
                    continue
                if p.get("score", 0) < min_score:
                    rej["min_score"] += 1
                    continue
                if nsfw is True and not p.get("over_18"):
                    rej["nsfw"] += 1
                    continue
                if nsfw is False and p.get("over_18"):
                    rej["nsfw"] += 1
                    continue
                if (min_upvote_ratio is not None
                        and p.get("upvote_ratio", 0) < min_upvote_ratio):
                    rej["min_upvote_ratio"] += 1
                    continue
            if excluded_set and p.get("subreddit", "").lower() in excluded_set:
                rej["excluded_sub"] += 1
                continue
            p["matched_keywords"] = hit_kw
            matched.append(p)

        rej_str = ", ".join(f"{k}={v}" for k, v in rej.items() if v) or "none"
        print(f"    Tight-window filter: kept {len(matched)} of {len(all_posts)} "
              f"(rejections: {rej_str})")

        # Fallback: if the recent-fetch source produced nothing usable
        # (RSS down/blocked AND Pullpush rate-limited/empty), fall back
        # to the standard per-keyword cascade which ALSO tries Arctic.
        # The production diagnostic showed Arctic returning real data
        # when both RSS and Pullpush returned 0 — without this fallback
        # the tight-window path would report 0 despite Arctic having
        # the posts. We pass force_full_cascade to avoid re-entering
        # this fast path (infinite loop guard).
        if not matched and not self._tight_window_recursion_guard:
            print(f"    ⚠ Tight-window yielded 0 — falling back to full "
                  f"per-keyword cascade (includes Arctic).")
            self._tight_window_recursion_guard = True
            try:
                fallback = self._full_cascade_multi(
                    keywords, subreddits, **kwargs
                )
            finally:
                self._tight_window_recursion_guard = False
            if fallback:
                return fallback

        # Sort by recency descending (this mode is fundamentally
        # "what's new") and balance across subs if multi-sub.
        matched.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return balance_posts_by_subreddit(matched, limit, subreddits)

    def _full_cascade_multi(self, keywords, subreddits, **kwargs):
        """Standard per-keyword concurrent search across the full
        cascade (reddit RSS → pullpush → arctic). Used as the
        tight-window fallback when the recent-fetch source came up
        empty. Mirrors the non-fast-path branch of
        search_multiple_keywords.
        """
        all_results = []
        seen_ids = set()
        # Re-attach subreddits into kwargs for the per-keyword search().
        inner = dict(kwargs)
        inner["subreddits"] = subreddits
        inner.pop("subreddit", None)
        limit = inner.get("limit", 50)

        def _one(kw):
            try:
                return self.search(kw, **inner)
            except Exception as e:
                print(f"    cascade fallback error ({kw}): {e}")
                return []

        with ThreadPoolExecutor(max_workers=min(len(keywords), 5)) as ex:
            futures = {ex.submit(_one, kw): kw for kw in keywords}
            for f in as_completed(futures):
                try:
                    for post in (f.result() or []):
                        pid = post.get("id")
                        if pid and pid not in seen_ids:
                            seen_ids.add(pid)
                            all_results.append(post)
                except Exception as e:
                    print(f"    cascade fallback merge error: {e}")
        all_results.sort(key=lambda x: str(x.get("id") or ""))
        all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return balance_posts_by_subreddit(all_results, limit, subreddits)

    def search_multiple_keywords(self, keywords, delay=2, concurrent=False, **kwargs):
        """
        Search for multiple keywords, combining and deduplicating results.

        Special fast path for tight time windows (max_days_old ≤ 7) WITH
        a sub list: bypass the per-(keyword × sub) fan-out and instead
        fetch the last N days of posts ONCE per sub (no keyword filter
        upstream), then run a client-side multi-keyword substring match.
        Saves O(K) Pullpush calls when querying N subs — which is the
        difference between "1 result" and "all of them" for niche
        keyword queries in 1-day windows where Pullpush's search index
        lags behind /new.
        """
        # NOTE: the tight-window fast path (_search_recent_then_filter)
        # is DISABLED. It fetched each sub's /new.rss (no keyword) and
        # matched keywords client-side with a naive substring test
        # (`kw in text`), which made short keywords like "hr" match
        # "through" / "chrome" / "Chris" → flooded results with junk →
        # mass relevance-skips during gen-comments.
        #
        # The regular per-keyword search() cascade below uses Reddit's
        # real search engine via /r/<sub>/search.rss?q=<kw>&t=<window>,
        # which does proper token matching AND honors the time window —
        # so days≤7 multi-sub searches now get accurate, relevant
        # results without the substring-noise problem. (The old reason
        # for the fast path — Pullpush's search-index lag at days=1 —
        # no longer applies now that search.rss is reachable.)
        # Set REDDIT_TIGHT_WINDOW=1 to opt back into the fast path.
        subreddits = kwargs.get("subreddits") or kwargs.get("subreddit")
        if isinstance(subreddits, str):
            subreddits = [subreddits]
        max_days_old = kwargs.get("max_days_old")
        if (os.environ.get("REDDIT_TIGHT_WINDOW", "").strip().lower() in ("1", "true", "yes")
                and max_days_old and 1 <= max_days_old <= 7
                and subreddits and len(subreddits) >= 1
                and len(keywords) > 1):
            print(f"\n⚡ Tight-window fast path (opt-in): max_days_old={max_days_old}, "
                  f"{len(subreddits)} subs × {len(keywords)} keywords.")
            inner_kwargs = {k: v for k, v in kwargs.items()
                            if k not in ("subreddit", "subreddits")}
            return self._search_recent_then_filter(
                keywords, subreddits, **inner_kwargs
            )

        all_results = []
        seen_ids = set()

        def _search_one(keyword):
            print(f"\n🔍 Searching for '{keyword}'...")
            return self.search(keyword, **kwargs)

        if concurrent and len(keywords) > 1:
            # Keep outer keyword concurrency at 5 for throughput.
            # Pullpush/Arctic rate limits are smoothed by the smaller
            # per-keyword sub fan-out (5 inner threads) below — that
            # bounds the global max at 5 × 5 = 25 concurrent requests,
            # which both services tolerate. Going lower (e.g. 3 × 3
            # = 9) starved throughput badly for 10-keyword queries.
            with ThreadPoolExecutor(max_workers=min(len(keywords), 5)) as executor:
                futures = {executor.submit(_search_one, kw): kw for kw in keywords}
                for future in as_completed(futures):
                    try:
                        for post in future.result():
                            with self._lock:
                                if post["id"] not in seen_ids:
                                    seen_ids.add(post["id"])
                                    all_results.append(post)
                    except Exception as e:
                        print(f"✗ Search error: {e}")
        else:
            for i, keyword in enumerate(keywords):
                for post in _search_one(keyword):
                    if post["id"] not in seen_ids:
                        seen_ids.add(post["id"])
                        all_results.append(post)
                if i < len(keywords) - 1:
                    time.sleep(delay)

        # Deterministic merge order (score desc, id) so the cross-keyword union
        # doesn't depend on which keyword's thread finished first (as_completed).
        all_results.sort(key=lambda x: str(x.get("id") or ""))
        all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return all_results

    @staticmethod
    def build_query(terms=None, phrases=None, any_of=None, exclude=None):
        """
        Build a Reddit-compatible boolean search query string.

        Args:
            terms:   Words that must ALL appear (AND logic).
            phrases: Exact phrases that must appear (quoted).
            any_of:  Words where ANY can match (OR logic).
            exclude: Words that must NOT appear.

        Returns:
            Query string suitable for the `keyword` argument of search().

        Examples:
            build_query(terms=["TRT"], phrases=["low testosterone"])
            -> 'TRT AND "low testosterone"'

            build_query(any_of=["TRT", "testosterone", "low T"], exclude=["female"])
            -> '(TRT OR testosterone OR "low T") NOT female'

            build_query(terms=["NDIS"], any_of=["provider", "plan manager"])
            -> 'NDIS AND (provider OR "plan manager")'
        """
        parts = []
        if terms:
            parts.extend(terms)
        if phrases:
            parts.extend(f'"{p}"' for p in phrases)

        query = " AND ".join(parts) if parts else ""

        if any_of:
            quoted = [f'"{w}"' if " " in w else w for w in any_of]
            or_clause = (
                f"({' OR '.join(quoted)})" if len(quoted) > 1 else quoted[0]
            )
            query = f"{query} AND {or_clause}" if query else or_clause

        if exclude:
            excl_str = " ".join(f"NOT {w}" for w in exclude)
            query = f"{query} {excl_str}".strip()

        return query

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------

    def save_to_json(self, results, filename="results.json"):
        """Save results to a JSON file."""
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"✓ Saved {len(results)} results to {filename}")

    def save_to_csv(self, results, filename="results.csv", max_rows=150):
        """Save results to CSV file(s), splitting into parts of max_rows each."""
        if not results:
            print("No results to save.")
            return
        base, ext = os.path.splitext(filename)
        if not ext:
            ext = ".csv"
        chunks = [results[i:i + max_rows] for i in range(0, len(results), max_rows)]
        if len(chunks) == 1:
            with open(f"{base}{ext}", "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=results[0].keys())
                writer.writeheader()
                writer.writerows(chunks[0])
            print(f"✓ Saved {len(results)} results to {base}{ext}")
        else:
            for idx, chunk in enumerate(chunks, 1):
                part_name = f"{base}_part{idx}{ext}"
                with open(part_name, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=results[0].keys())
                    writer.writeheader()
                    writer.writerows(chunk)
                print(f"✓ Saved {len(chunk)} results to {part_name}")
            print(f"✓ Total: {len(results)} results across {len(chunks)} files")

    def print_results(self, results, verbose=False):
        """Print results to console in a readable format."""
        if not results:
            print("No results found.")
            return

        print(f"\n{'='*80}")
        print(f"Found {len(results)} matching posts (via {self.current_api})")
        print(f"{'='*80}\n")

        for i, post in enumerate(results, 1):
            nsfw_tag = " [NSFW]" if post.get("over_18") else ""
            print(f"{i}. {post['title']}{nsfw_tag}")
            print(
                f"   📊 Score: {post['score']} | "
                f"💬 Comments: {post['comments']} | "
                f"📁 r/{post['subreddit']}"
            )
            print(f"   📅 {post['date']} | 👤 u/{post['author']}")
            ratio = post.get("upvote_ratio", 0)
            if ratio:
                print(
                    f"   👍 {ratio:.0%} upvoted | "
                    f"Flair: {post.get('flair') or 'None'}"
                )
            print(f"   🔗 {post['url']}")

            if verbose and post.get("text"):
                preview = post["text"][:200].replace("\n", " ")
                print(f"   📝 {preview}...")

            print()

        # Subreddit summary
        sub_counts = Counter(post.get("subreddit", "unknown") for post in results)
        print(f"{'─'*40}")
        print(f"Subreddits tracked ({len(sub_counts)}):")
        for sub, count in sub_counts.most_common():
            print(f"   r/{sub}: {count} posts")
        print(f"{'─'*40}")


# ======================================================================
# CLI entry point
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Search Reddit for posts (no API key required, multiple fallback APIs)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python reddit_bot.py "python tutorial" --min-score 100 --days 7
  python reddit_bot.py "TRT clinic" --subreddit testosterone --days 30
  python reddit_bot.py "low T" --subreddits testosterone,trt,malehealth --limit 200
  python reddit_bot.py "NDIS provider" --exclude-subreddits AskReddit,worldnews
  python reddit_bot.py "HRT" --nsfw exclude --min-upvote-ratio 0.7
        """,
    )

    # Required
    parser.add_argument("keyword", help="Search keyword or topic")

    # Subreddit targeting
    sr_group = parser.add_mutually_exclusive_group()
    sr_group.add_argument("--subreddit", "-s", default=None,
                          help="Single subreddit to search (default: all)")
    sr_group.add_argument("--subreddits", default=None,
                          help="Comma-separated subreddits, e.g. testosterone,trt,malehealth")
    parser.add_argument("--exclude-subreddits", default=None,
                        help="Comma-separated subreddits to exclude from results")

    # Filters
    parser.add_argument("--min-comments", "-c", type=int, default=0,
                        help="Minimum number of comments")
    parser.add_argument("--min-score", "-sc", type=int, default=0,
                        help="Minimum score/upvotes")
    parser.add_argument("--days", "-d", type=int, default=None,
                        help="Maximum post age in days")
    parser.add_argument("--sort-by",
                        choices=["score", "comments", "new", "relevance"],
                        default="relevance", help="Sort field")
    parser.add_argument("--sort-order",
                        choices=["desc", "asc"],
                        default="desc",
                        help="Sort direction: desc (new-to-old, default) or asc (old-to-new)")
    parser.add_argument("--limit", "-l", type=int, default=100,
                        help="Maximum posts to fetch (default: 100)")
    parser.add_argument("--api",
                        choices=["auto", "reddit", "pullpush", "arctic"],
                        default="auto", help="Which API to use")
    parser.add_argument("--nsfw",
                        choices=["include", "exclude", "only"],
                        default="include",
                        help="NSFW content: include (default), exclude, or only")
    parser.add_argument("--min-upvote-ratio", type=float, default=None,
                        help="Minimum upvote ratio 0.0–1.0 (e.g. 0.7)")

    # Output
    parser.add_argument("--output", "-o",
                        choices=["print", "json", "csv", "all"],
                        default="print", help="Output format")
    parser.add_argument("--filename", "-f", default="reddit_results",
                        help="Output filename (without extension)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show post preview text")

    args = parser.parse_args()

    # Parse subreddits
    subreddits_list = None
    if args.subreddits:
        subreddits_list = [s.strip() for s in args.subreddits.split(",") if s.strip()]

    excluded_list = None
    if args.exclude_subreddits:
        excluded_list = [s.strip() for s in args.exclude_subreddits.split(",") if s.strip()]

    # Map nsfw flag
    nsfw_map = {"include": None, "exclude": False, "only": True}
    nsfw = nsfw_map[args.nsfw]

    bot = RedditSearchBot()

    print(f"\n🔍 Searching for '{args.keyword}'...")
    if subreddits_list:
        print(f"   in r/{'+'.join(subreddits_list)}")
    elif args.subreddit:
        print(f"   in r/{args.subreddit}")
    if excluded_list:
        print(f"   excluding: r/{', r/'.join(excluded_list)}")

    results = bot.search(
        keyword=args.keyword,
        subreddit=args.subreddit,
        subreddits=subreddits_list,
        excluded_subreddits=excluded_list,
        min_comments=args.min_comments,
        min_score=args.min_score,
        max_days_old=args.days,
        sort_by=args.sort_by,
        sort_order=args.sort_order,
        limit=args.limit,
        api=args.api,
        nsfw=nsfw,
        min_upvote_ratio=args.min_upvote_ratio,
    )

    if args.output in ["print", "all"]:
        bot.print_results(results, verbose=args.verbose)
    if args.output in ["json", "all"]:
        bot.save_to_json(results, f"{args.filename}.json")
    if args.output in ["csv", "all"]:
        bot.save_to_csv(results, f"{args.filename}.csv")

    # Hint about restriction checking (now a separate tool)
    if args.output in ["csv", "all"] and results:
        print(f"\n💡 To add restriction data, run:")
        print(f"   python3 restriction_bot.py --csv {args.filename}.csv")


if __name__ == "__main__":
    main()
