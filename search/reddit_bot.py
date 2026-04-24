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
from collections import Counter
import requests
from datetime import datetime, timedelta
import json
import csv
import argparse
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


def balance_posts_by_subreddit(posts, limit, subreddits):
    """Cut a sorted list of posts down to `limit` with equal priority per
    subreddit.

    When multiple subreddits are requested, this takes up to `limit // N`
    posts from each (preserving the input order within each sub, which is
    assumed to already be sorted by score / comments / date / relevance).
    Any remaining slots are filled with the top-scoring overflow posts.

    When `subreddits` is None or length 1, or when the input is already at
    or under the limit, returns `posts[:limit]` unchanged.

    Callers: used both inside a single keyword search (after its own filter
    stage) and at the multi-keyword merge stage so the final output is
    balanced end-to-end, not just per-keyword.
    """
    if not subreddits or len(subreddits) <= 1 or len(posts) <= limit:
        return posts[:limit]

    n = len(subreddits)
    per_sub = limit // n
    by_sub = {}
    for p in posts:
        key = (p.get("subreddit") or "").lower()
        by_sub.setdefault(key, []).append(p)

    result = []
    leftover = []
    for sub in by_sub:
        result.extend(by_sub[sub][:per_sub])
        leftover.extend(by_sub[sub][per_sub:])
    remaining = limit - len(result)
    if remaining > 0:
        leftover.sort(key=lambda x: x.get("score", 0), reverse=True)
        result.extend(leftover[:remaining])
    return result


class RedditSearchBot:
    def __init__(self, retry_attempts=2, retry_delay=1, reddit_base=None):
        """Initialize the Reddit bot with multiple API endpoints."""
        self.apis = {
            "reddit": reddit_base or "https://www.reddit.com",
            "pullpush": "https://api.pullpush.io/reddit/search/submission",
            "arctic": "https://arctic-shift.photon-reddit.com/api/posts/search",
        }
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        self.retry_attempts = retry_attempts
        self.retry_delay = retry_delay
        self.current_api = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_request(self, url, params, timeout=30):
        """GET request with exponential backoff on rate limits (HTTP 429)."""
        last_error = None
        for attempt in range(self.retry_attempts):
            try:
                response = requests.get(
                    url, params=params, headers=self.headers, timeout=timeout
                )
                if response.status_code == 429:
                    wait = self.retry_delay * (2 ** attempt)
                    print(f"\n    ⏳ Rate limited. Retrying in {wait}s...", end=" ", flush=True)
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                return response
            except requests.exceptions.RequestException as e:
                last_error = e
                if attempt < self.retry_attempts - 1:
                    time.sleep(self.retry_delay * (2 ** attempt))
        raise last_error

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
            data = response.json()
            posts = data.get("data", {}).get("children", [])
            after = data.get("data", {}).get("after")

            if not posts:
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

    def _search_pullpush(self, keyword, subreddit, sort_by, max_days_old, limit,
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

        while len(results) < limit:
            batch_size = min(100, limit - len(results))
            params = {
                "q": keyword,
                "size": batch_size,
                "sort": sort_order,
                "sort_type": sort_by,
            }
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
                print(f"({e.__class__.__name__}: {str(e)[:50]})")
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
        """Arctic-Shift API with timestamp-based pagination.

        NOTE: Arctic-Shift requires a subreddit filter when using text search.
        Returns empty list immediately if no subreddit is specified.
        """
        if not subreddit:
            return []  # Arctic-Shift requires a subreddit for text queries

        results = []
        seen_ids = set()
        before_ts = None

        while len(results) < limit:
            batch_size = min(100, limit - len(results))
            params = {"query": keyword, "subreddit": subreddit, "limit": batch_size}
            if max_days_old:
                after_date = datetime.utcnow() - timedelta(days=max_days_old)
                params["after"] = after_date.strftime("%Y-%m-%d")
            if before_ts:
                params["before"] = str(int(before_ts))

            try:
                response = self._make_request(self.apis["arctic"], params)
                data = response.json()
                # Arctic-Shift returns errors in the response body
                if data.get("error"):
                    print(f"(Arctic-Shift error: {data['error'][:50]})")
                    break
            except Exception as e:
                print(f"({e.__class__.__name__}: {str(e)[:50]})")
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
    ):
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

        apis_to_try = ["reddit", "pullpush", "arctic"] if api == "auto" else [api]
        filtered = []   # only posts that pass all filters
        seen_ids = set()

        # Detect whether strict filters are active (high rejection rate expected)
        has_strict_filters = (min_comments > 0 or min_score > 0
                              or min_upvote_ratio is not None
                              or max_subscribers is not None
                              or min_subscribers is not None
                              or nsfw is not None or excluded_set)

        for api_name in apis_to_try:
            # Stop early if we already have enough results
            if len(filtered) >= limit:
                break

            # Adaptive over-fetch: 5x when filters are active (they reject
            # 60-80% of raw posts), 2x otherwise.
            multiplier = 5 if has_strict_filters else 2
            floor = 500 if has_strict_filters else 100
            needed_raw = max((limit - len(filtered)) * multiplier, floor)
            try:
                print(f"    Trying {api_name} API...", end=" ", flush=True)

                if api_name == "reddit":
                    if subreddit_path and "+" in subreddit_path:
                        # Multi-sub: query each subreddit individually so every
                        # one gets an equal quota, rather than letting Reddit's
                        # combined-sub search (r/a+b+c) decide the mix (which
                        # biases heavily toward the highest-scoring sub and can
                        # starve the others). Fan out the per-sub fetches in
                        # parallel to avoid serial network latency.
                        subs = [s.strip() for s in subreddit_path.split("+")
                                if s.strip()]
                        batch = []
                        per_sub = max(needed_raw // max(len(subs), 1), 25)
                        with ThreadPoolExecutor(max_workers=min(len(subs), 8)) as ex:
                            futures = [
                                ex.submit(self._search_reddit_native,
                                          keyword, sub, reddit_sort, time_filter, per_sub)
                                for sub in subs
                            ]
                            for f in as_completed(futures):
                                try:
                                    batch.extend(f.result() or [])
                                except Exception as e:
                                    print(f"    per-sub fetch error (reddit): {e}")
                    else:
                        batch = self._search_reddit_native(
                            keyword, subreddit_path, reddit_sort, time_filter, needed_raw
                        )
                elif api_name == "pullpush":
                    if subreddit_path and "+" in subreddit_path:
                        # Multi-sub: Pullpush only accepts a single subreddit,
                        # so split and query each with equal quota — in parallel.
                        subs = [s.strip() for s in subreddit_path.split("+")
                                if s.strip()]
                        batch = []
                        per_sub = max(needed_raw // max(len(subs), 1), 50)
                        with ThreadPoolExecutor(max_workers=min(len(subs), 8)) as ex:
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
                    else:
                        batch = self._search_pullpush(
                            keyword, subreddit_path, sort_by, max_days_old,
                            needed_raw, sort_order=sort_order,
                        )
                elif api_name == "arctic":
                    if subreddit_path and "+" not in subreddit_path:
                        # Single subreddit — query directly
                        batch = self._search_arctic(
                            keyword, subreddit_path, max_days_old, needed_raw
                        )
                    elif not subreddit_path:
                        # Global search — discover subreddits from prior
                        # results, then query Arctic-Shift per subreddit in parallel.
                        discovered_subs = {}
                        sub_counts = {}
                        for post in filtered:
                            sub = post.get("subreddit", "")
                            if sub:
                                key = sub.lower()
                                discovered_subs[key] = sub
                                sub_counts[key] = sub_counts.get(key, 0) + 1
                        top_subs = sorted(
                            sub_counts, key=sub_counts.get, reverse=True
                        )[:10]

                        batch = []
                        per_sub = max(needed_raw // max(len(top_subs), 1), 50)
                        if top_subs:
                            with ThreadPoolExecutor(max_workers=min(len(top_subs), 8)) as ex:
                                futures = [
                                    ex.submit(self._search_arctic,
                                              keyword, discovered_subs[sub_key],
                                              max_days_old, per_sub)
                                    for sub_key in top_subs
                                ]
                                for f in as_completed(futures):
                                    try:
                                        batch.extend(f.result() or [])
                                    except Exception as e:
                                        print(f"    per-sub fetch error (arctic-global): {e}")
                            print(f"(queried {len(top_subs)} subs) ", end="")
                    else:
                        # Multi-sub path (a+b+c) — query each individually in parallel.
                        subs = [s.strip() for s in subreddit_path.split("+")
                                if s.strip()]
                        batch = []
                        per_sub = max(needed_raw // max(len(subs), 1), 50)
                        with ThreadPoolExecutor(max_workers=min(len(subs), 8)) as ex:
                            futures = [
                                ex.submit(self._search_arctic,
                                          keyword, sub, max_days_old, per_sub)
                                for sub in subs
                            ]
                            for f in as_completed(futures):
                                try:
                                    batch.extend(f.result() or [])
                                except Exception as e:
                                    print(f"    per-sub fetch error (arctic): {e}")
                else:
                    batch = []

                # Apply all filters inline — only count posts that pass
                new_count = 0
                for post in batch:
                    pid = post.get("id", "")
                    if not pid or pid in seen_ids:
                        continue
                    if post["comments"] < min_comments:
                        continue
                    if post["score"] < min_score:
                        continue
                    if cutoff_date and post["timestamp"]:
                        try:
                            if datetime.utcfromtimestamp(post["timestamp"]) < cutoff_date:
                                continue
                        except (OSError, OverflowError, ValueError):
                            pass
                    if excluded_set and post.get("subreddit", "").lower() in excluded_set:
                        continue
                    if nsfw is True and not post.get("over_18"):
                        continue
                    if nsfw is False and post.get("over_18"):
                        continue
                    if (
                        min_upvote_ratio is not None
                        and post.get("upvote_ratio", 0) < min_upvote_ratio
                    ):
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
                    print(f"No new results ({batch_total} raw, all filtered out)")

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
            filtered.sort(key=lambda x: x.get(key, 0), reverse=(sort_order == "desc"))

        # Equal distribution across subreddits when multiple are searched
        return balance_posts_by_subreddit(filtered, limit, subreddits)

    # Class-level subscriber cache (persists across searches within same process)
    _sub_cache = {}  # sub_name_lower -> (subscriber_count, timestamp)
    _SUB_CACHE_TTL = 3600  # 1 hour

    def _filter_by_subscribers(self, results, max_subscribers, min_subscribers=None):
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

    def search_multiple_keywords(self, keywords, delay=2, concurrent=False, **kwargs):
        """
        Search for multiple keywords, combining and deduplicating results.

        Args:
            keywords: List of search terms.
            delay: Seconds between searches (sequential mode only).
            concurrent: Run searches in parallel threads (faster, but more
                        connections). Max 3 workers to stay API-friendly.
            **kwargs: Passed directly to search().

        Returns:
            Combined, deduplicated list sorted by score descending.
        """
        all_results = []
        seen_ids = set()

        def _search_one(keyword):
            print(f"\n🔍 Searching for '{keyword}'...")
            return self.search(keyword, **kwargs)

        if concurrent and len(keywords) > 1:
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

        all_results.sort(key=lambda x: x["score"], reverse=True)
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
