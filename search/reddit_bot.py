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
                    batch = self._search_reddit_native(
                        keyword, subreddit_path, reddit_sort, time_filter, needed_raw
                    )
                elif api_name == "pullpush":
                    if subreddit_path and "+" in subreddit_path:
                        # Multi-sub: Pullpush only accepts a single subreddit,
                        # so split and query each with equal quota.
                        subs = [s.strip() for s in subreddit_path.split("+")
                                if s.strip()]
                        batch = []
                        per_sub = max(needed_raw // max(len(subs), 1), 50)
                        for sub in subs:
                            sub_batch = self._search_pullpush(
                                keyword, sub, sort_by, max_days_old, per_sub,
                                sort_order=sort_order,
                            )
                            batch.extend(sub_batch)
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
                        # results, then query Arctic-Shift per subreddit
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
                        for sub_key in top_subs:
                            sub_batch = self._search_arctic(
                                keyword, discovered_subs[sub_key],
                                max_days_old, per_sub,
                            )
                            batch.extend(sub_batch)
                        if top_subs:
                            print(f"(queried {len(top_subs)} subs) ", end="")
                    else:
                        # Multi-sub path (a+b+c) — query each individually
                        subs = [s.strip() for s in subreddit_path.split("+")
                                if s.strip()]
                        batch = []
                        per_sub = max(needed_raw // max(len(subs), 1), 50)
                        for sub in subs:
                            sub_batch = self._search_arctic(
                                keyword, sub, max_days_old, per_sub,
                            )
                            batch.extend(sub_batch)
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
            if max_subscribers and filtered:
                before_sub = len(filtered)
                filtered = self._filter_by_subscribers(filtered, max_subscribers)
                removed = before_sub - len(filtered)
                if removed:
                    print(f"    Subscriber filter: removed {removed}, {len(filtered)} remaining")

        if not filtered:
            print("    ⚠ All APIs failed or returned no results")
            return []

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

        return filtered[:limit]

    # Class-level subscriber cache (persists across searches within same process)
    _sub_cache = {}  # sub_name_lower -> (subscriber_count, timestamp)
    _SUB_CACHE_TTL = 3600  # 1 hour

    def _filter_by_subscribers(self, results, max_subscribers):
        """Filter results to only include posts from subs with ≤ max_subscribers.

        Fetches subscriber counts for each unique subreddit via Reddit API.
        Uses in-memory cache to avoid redundant lookups.
        Attaches sub_subscribers to each result for frontend display.
        Posts from subs where the count can't be determined are kept.
        """
        unique_subs = list(set(r.get("subreddit", "") for r in results if r.get("subreddit")))
        print(f"    Checking subscriber counts for {len(unique_subs)} subreddits (max: {max_subscribers:,})...")

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

        # Attach subscriber count to each result & filter
        filtered = []
        for r in results:
            count = sub_info.get(r.get("subreddit", "").lower())
            r["sub_subscribers"] = count
            if count is None or count <= max_subscribers:
                filtered.append(r)

        removed = len(results) - len(filtered)
        print(f"    Subscriber filter: kept {len(filtered)}, removed {removed} (from subs >{max_subscribers:,})")
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
            with ThreadPoolExecutor(max_workers=min(len(keywords), 3)) as executor:
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
