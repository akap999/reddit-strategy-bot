"""SQLite persistence layer for Reddit Strategy Bot."""

import sqlite3
import json
import os
import re
from datetime import datetime, timedelta


class Database:
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = None

    def connect(self):
        self.conn = sqlite3.connect(self.db_path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def initialize(self):
        """Create all tables if they don't exist."""
        if not self.conn:
            self.connect()

        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS subreddits (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT UNIQUE NOT NULL,
                domain          TEXT NOT NULL,
                description     TEXT,
                rules           TEXT,
                sidebar         TEXT,
                welcome_message TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS brands (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                subreddit_id    INTEGER NOT NULL REFERENCES subreddits(id),
                name            TEXT NOT NULL,
                domain_url      TEXT,
                context         TEXT NOT NULL,
                keywords        TEXT,
                added_at        TEXT DEFAULT (datetime('now')),
                UNIQUE(subreddit_id, name)
            );

            CREATE TABLE IF NOT EXISTS post_urls (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id      INTEGER REFERENCES posts(id),
                subreddit_id INTEGER NOT NULL REFERENCES subreddits(id),
                reddit_url   TEXT NOT NULL UNIQUE,
                added_at     TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS posts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                subreddit_id    INTEGER NOT NULL REFERENCES subreddits(id),
                brand_id        INTEGER REFERENCES brands(id),
                title           TEXT NOT NULL,
                body            TEXT NOT NULL,
                storyline       TEXT NOT NULL,
                image_prompt    TEXT,
                image_url       TEXT,
                ai_query_score  INTEGER DEFAULT 0,
                is_custom       INTEGER DEFAULT 0,
                is_filler       INTEGER DEFAULT 0,
                status          TEXT DEFAULT 'draft',
                suggested_post_day INTEGER DEFAULT 0,
                prompt_version  TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS comments (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id           INTEGER NOT NULL REFERENCES posts(id),
                brand_id          INTEGER REFERENCES brands(id),
                account_id        TEXT,
                body              TEXT NOT NULL,
                persona_id        TEXT,
                structure_id      TEXT,
                is_reply          INTEGER DEFAULT 0,
                parent_comment_id INTEGER REFERENCES comments(id),
                mentions_brand    INTEGER DEFAULT 0,
                validation_score  REAL,
                status            TEXT DEFAULT 'draft',
                suggested_post_day INTEGER DEFAULT 0,
                suggested_order   INTEGER DEFAULT 0,
                prompt_version    TEXT,
                created_at        TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS accounts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT UNIQUE NOT NULL,
                link_karma      INTEGER DEFAULT 0,
                comment_karma   INTEGER DEFAULT 0,
                created_utc     REAL,
                reference       TEXT DEFAULT '',
                added_at        TEXT DEFAULT (datetime('now')),
                last_refreshed  TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_posts_sub_brand ON posts(subreddit_id, brand_id);
            CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
            CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
            CREATE INDEX IF NOT EXISTS idx_comments_status ON comments(status);
            CREATE INDEX IF NOT EXISTS idx_post_urls_sub ON post_urls(subreddit_id);
            CREATE INDEX IF NOT EXISTS idx_accounts_username ON accounts(username);

            CREATE TABLE IF NOT EXISTS post_brands (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id  INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                brand_id INTEGER NOT NULL REFERENCES brands(id),
                UNIQUE(post_id, brand_id)
            );
            CREATE INDEX IF NOT EXISTS idx_post_brands_post ON post_brands(post_id);
            CREATE INDEX IF NOT EXISTS idx_post_brands_brand ON post_brands(brand_id);

            CREATE TABLE IF NOT EXISTS blogs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id         INTEGER REFERENCES brands(id),
                seed             TEXT NOT NULL,
                title            TEXT,
                meta_description TEXT,
                keywords         TEXT,
                body_markdown    TEXT,
                linkedin_text    TEXT,
                claims_flagged   TEXT,
                source_urls      TEXT,
                research_notes   TEXT,
                use_web_search   INTEGER DEFAULT 0,
                status           TEXT DEFAULT 'draft',
                prompt_version   TEXT,
                created_at       TEXT DEFAULT (datetime('now')),
                updated_at       TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS blog_platforms (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                blog_id       INTEGER NOT NULL REFERENCES blogs(id) ON DELETE CASCADE,
                platform      TEXT NOT NULL,
                published_url TEXT,
                published_at  TEXT,
                status        TEXT DEFAULT 'draft',
                UNIQUE(blog_id, platform)
            );
            CREATE INDEX IF NOT EXISTS idx_blogs_brand ON blogs(brand_id);
            CREATE INDEX IF NOT EXISTS idx_blogs_status ON blogs(status);
            CREATE INDEX IF NOT EXISTS idx_blog_platforms_blog ON blog_platforms(blog_id);
        """)
        self.conn.commit()
        self._run_migrations()

    # --- Subreddits ---

    def create_subreddit(self, name, domain, description="", rules="[]", sidebar="", welcome_message=""):
        cur = self.conn.execute(
            "INSERT INTO subreddits (name, domain, description, rules, sidebar, welcome_message) VALUES (?, ?, ?, ?, ?, ?)",
            (name, domain, description, rules, sidebar, welcome_message)
        )
        self.conn.commit()
        return cur.lastrowid

    def list_subreddits(self, live=False):
        """List subreddits. By default excludes Live Subreddits (is_live=1) so
        the regular Subreddits page stays clean. Pass live=True to fetch only
        live subs, or live=None for both."""
        where = ""
        params = []
        if live is False:
            where = "WHERE COALESCE(s.is_live, 0) = 0 "
        elif live is True:
            where = "WHERE COALESCE(s.is_live, 0) = 1 "
        rows = self.conn.execute(f"""
            SELECT s.*,
                   COUNT(DISTINCT b.id) as brand_count,
                   COUNT(DISTINCT CASE WHEN p.status IN ('published', 'paid') THEN p.id END) as post_count,
                   COUNT(DISTINCT CASE WHEN c.status IN ('deployed', 'paid') THEN c.id END) as comment_count
            FROM subreddits s
            LEFT JOIN brands b ON b.subreddit_id = s.id
            LEFT JOIN posts p ON p.subreddit_id = s.id
            LEFT JOIN comments c ON c.post_id = p.id
            {where}
            GROUP BY s.id
            ORDER BY s.created_at DESC
        """, params).fetchall()
        return [dict(r) for r in rows]

    def get_subreddit(self, subreddit_id):
        row = self.conn.execute("SELECT * FROM subreddits WHERE id = ?", (subreddit_id,)).fetchone()
        return dict(row) if row else None

    def ensure_live_subreddit(self, name):
        """Auto-provision a subreddits row for a brand-driven Live Subreddits flow.

        Returns the existing row if `name` already exists; otherwise creates a
        minimal row with is_live=1 (so the rest of the post pipeline, which
        requires a real subreddits.id FK, just works) and returns it.
        Name is normalized: leading r/ stripped, lowercased.
        """
        clean = (name or "").strip()
        if clean.lower().startswith("r/"):
            clean = clean[2:]
        clean = clean.strip("/").lower()
        if not clean:
            return None
        row = self.conn.execute(
            "SELECT * FROM subreddits WHERE LOWER(name) = ?", (clean,)
        ).fetchone()
        if row:
            return dict(row)
        self.conn.execute(
            "INSERT INTO subreddits (name, domain, description, rules, sidebar, welcome_message, is_live) "
            "VALUES (?, '', '', '[]', '', '', 1)",
            (clean,)
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM subreddits WHERE LOWER(name) = ?", (clean,)
        ).fetchone()
        return dict(row) if row else None

    # Reserved name for the "no subreddit yet" holding pool. Posts
    # generated from brand context (no subreddit picked) are saved under
    # this row so the NOT-NULL posts.subreddit_id FK is satisfied; the
    # operator assigns a real subreddit later via PUT /api/posts/<id>/subreddit.
    UNASSIGNED_SUBREDDIT = "unassigned"

    def ensure_unassigned_subreddit(self):
        """Get-or-create the shared 'unassigned' holding-pool subreddit row.

        Used as the save target for posts generated without a subreddit.
        It's a normal is_live=1 row, but the Live Subs subreddit pickers
        list a brand's saved subreddits (not this), so it won't clutter
        them — it only surfaces as an 'unassigned' group in the posts table.
        """
        return self.ensure_live_subreddit(self.UNASSIGNED_SUBREDDIT)

    def get_subreddit_by_name(self, name):
        row = self.conn.execute("SELECT * FROM subreddits WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    # --- Brands ---

    def add_brand(self, subreddit_id, name, domain_url="", context="", keywords="[]",
                  category=None, audience=None, use_cases=None, pain_points=None,
                  features=None, competitors=None, enriched_at=None,
                  search_subreddits=None, focus=None, competitor_domains=None,
                  author_name=None, author_title=None, reviewer_name=None,
                  reviewer_title=None, disclosure=None):
        cur = self.conn.execute(
            """INSERT INTO brands (subreddit_id, name, domain_url, context, keywords,
                                   category, audience, use_cases, pain_points,
                                   features, competitors, enriched_at, search_subreddits,
                                   focus, competitor_domains, author_name, author_title,
                                   reviewer_name, reviewer_title, disclosure)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (subreddit_id, name, domain_url, context, keywords,
             category, audience, use_cases, pain_points,
             features, competitors, enriched_at, search_subreddits,
             focus, competitor_domains, author_name, author_title,
             reviewer_name, reviewer_title, disclosure)
        )
        self.conn.commit()
        return cur.lastrowid

    def update_brand(self, brand_id, context=None, domain_url=None, keywords=None,
                     category=None, audience=None, use_cases=None, pain_points=None,
                     features=None, competitors=None, enriched_at=None,
                     search_subreddits=None, focus=None, learned_context=None,
                     personas=None, competitor_domains=None, author_name=None,
                     author_title=None, reviewer_name=None, reviewer_title=None,
                     disclosure=None):
        """Update a brand's editable fields. Pass only the fields you want to change."""
        updates = []
        params = []
        field_map = {
            "context": context, "domain_url": domain_url, "keywords": keywords,
            "category": category, "audience": audience, "use_cases": use_cases,
            "pain_points": pain_points, "features": features,
            "competitors": competitors, "enriched_at": enriched_at,
            "search_subreddits": search_subreddits,
            "focus": focus, "learned_context": learned_context,
            "personas": personas, "competitor_domains": competitor_domains,
            "author_name": author_name, "author_title": author_title,
            "reviewer_name": reviewer_name, "reviewer_title": reviewer_title,
            "disclosure": disclosure,
        }
        for col, val in field_map.items():
            if val is not None:
                updates.append(f"{col} = ?")
                params.append(val)
        if not updates:
            return
        params.append(brand_id)
        self.conn.execute(f"UPDATE brands SET {', '.join(updates)} WHERE id = ?", params)
        self.conn.commit()

    def list_brands(self, subreddit_id):
        """List brands for a specific subreddit only."""
        rows = self.conn.execute(
            "SELECT * FROM brands WHERE subreddit_id = ? ORDER BY added_at DESC",
            (subreddit_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_brand(self, brand_id):
        row = self.conn.execute("SELECT * FROM brands WHERE id = ?", (brand_id,)).fetchone()
        return dict(row) if row else None

    def get_brand_by_name(self, subreddit_id, name):
        row = self.conn.execute(
            "SELECT * FROM brands WHERE subreddit_id = ? AND name = ?",
            (subreddit_id, name)
        ).fetchone()
        return dict(row) if row else None

    # --- Post URLs ---

    def add_post_url(self, subreddit_id, reddit_url, post_id=None):
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO post_urls (subreddit_id, reddit_url, post_id) VALUES (?, ?, ?)",
            (subreddit_id, reddit_url, post_id)
        )
        self.conn.commit()
        return cur.lastrowid

    def get_post_urls(self, subreddit_id):
        rows = self.conn.execute(
            "SELECT * FROM post_urls WHERE subreddit_id = ? ORDER BY added_at DESC",
            (subreddit_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_url_for_post(self, post_id):
        row = self.conn.execute(
            "SELECT reddit_url FROM post_urls WHERE post_id = ?", (post_id,)
        ).fetchone()
        return row["reddit_url"] if row else None

    def link_url_to_post(self, post_id, reddit_url, subreddit_id):
        """Link a Reddit URL to a generated post (after manual publishing).

        Re-publishing a post with a NEW URL must replace the previous link,
        not accumulate. The previous behavior left orphan rows in post_urls,
        and `get_url_for_post` (which returns the first match) ended up
        returning the OLD url after an undeploy+redeploy with a different
        link.

        Now we always clear any existing post_urls for this post_id before
        creating/repointing the row for the new URL.
        """
        # Drop any prior URL rows that point to this post — undeploy may
        # not have cleaned them up.
        self.conn.execute(
            "DELETE FROM post_urls WHERE post_id = ?", (post_id,)
        )
        # If the same URL was previously linked to a DIFFERENT post, repoint
        # that row; otherwise insert a fresh one.
        existing = self.conn.execute(
            "SELECT id FROM post_urls WHERE reddit_url = ?", (reddit_url,)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE post_urls SET post_id = ?, subreddit_id = ? WHERE reddit_url = ?",
                (post_id, subreddit_id, reddit_url)
            )
        else:
            self.conn.execute(
                "INSERT INTO post_urls (subreddit_id, reddit_url, post_id) VALUES (?, ?, ?)",
                (subreddit_id, reddit_url, post_id)
            )
        self.conn.commit()

    def set_hq_anchor_url(self, post_id, url):
        """Set the Reddit URL of a post's HQ ANCHOR comment (comment_type='hq',
        parent_comment_id IS NULL) — so the post link and the HQ anchor link can be entered
        from the same place when deploying/reporting a post.

        Resolves the anchor regardless of status (prefers one that already has a URL, else the
        earliest). If the anchor is still pre-deploy (draft/complete/assigned/informed) it's
        DEPLOYED with this URL (status → 'deployed'); otherwise the URL is updated in place and
        the status is preserved (never downgrades a deployed/paid/report row). Blank url or no
        anchor → no-op. Returns the anchor comment id, or None."""
        url = (url or "").strip()
        if not url or not post_id:
            return None
        row = self.conn.execute(
            """SELECT id, status FROM comments
                WHERE post_id = ? AND comment_type = 'hq' AND parent_comment_id IS NULL
                ORDER BY (CASE WHEN TRIM(COALESCE(reddit_comment_url,'')) != '' THEN 0 ELSE 1 END),
                         COALESCE(posted_at, deployed_at, created_at)
                LIMIT 1""",
            (post_id,)
        ).fetchone()
        if not row:
            return None
        cid = row["id"]
        if (row["status"] or "").lower() in ("draft", "complete", "assigned", "informed"):
            self.deploy_comment(cid, url)          # sets url + deployed_at + status='deployed'
        else:
            self.update_comment_url(cid, url)      # url only; preserve deployed/paid/report/…
        return cid

    # --- Post-Brand Junction ---

    def add_post_brands(self, post_id, brand_ids):
        """Link a post to multiple brands via the junction table."""
        for bid in brand_ids:
            self.conn.execute(
                "INSERT OR IGNORE INTO post_brands (post_id, brand_id) VALUES (?, ?)",
                (post_id, bid)
            )
        self.conn.commit()

    def get_brands_for_post(self, post_id):
        """Return list of brand dicts associated with a post."""
        rows = self.conn.execute(
            """SELECT b.* FROM brands b
               JOIN post_brands pb ON pb.brand_id = b.id
               WHERE pb.post_id = ?
               ORDER BY b.name""",
            (post_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Posts ---

    def save_post(self, subreddit_id, brand_id, title, body, storyline,
                  image_prompt=None, image_url=None, ai_query_score=0,
                  is_custom=0, is_filler=0, status="draft",
                  suggested_post_day=0, prompt_version=None, brand_ids=None,
                  intent=None, concept_checklist=None, ai_search_meta=None):
        # Inherit is_live from the parent subreddit so list/aggregation queries
        # can cheaply filter live vs. regular without re-joining subreddits.
        sub_row = self.conn.execute(
            "SELECT is_live FROM subreddits WHERE id = ?", (subreddit_id,)
        ).fetchone()
        is_live = 1 if (sub_row and sub_row["is_live"]) else 0
        # Stable per-brand post number (live posts only): next in this brand's
        # sequence. NULL for non-live posts.
        post_number = None
        if is_live:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(post_number),0)+1 AS n FROM posts "
                "WHERE brand_id IS ? AND COALESCE(is_live,0)=1",
                (brand_id,)
            ).fetchone()
            post_number = row["n"] if row else 1
        cur = self.conn.execute(
            """INSERT INTO posts (subreddit_id, brand_id, title, body, storyline,
               image_prompt, image_url, ai_query_score, is_custom, is_filler,
               status, suggested_post_day, prompt_version, intent, is_live,
               concept_checklist, ai_search_meta, post_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (subreddit_id, brand_id, title, body, storyline,
             image_prompt, image_url, ai_query_score, is_custom, is_filler,
             status, suggested_post_day, prompt_version, intent, is_live,
             concept_checklist, ai_search_meta, post_number)
        )
        post_id = cur.lastrowid
        # Populate junction table
        ids = brand_ids or ([brand_id] if brand_id else [])
        if ids:
            self.add_post_brands(post_id, ids)
        self.conn.commit()
        return post_id

    def get_posts(self, subreddit_id, brand_id=None, limit=50, include_filler=True, live=None):
        """Posts for a single subreddit. `live` is normally None (the
        subreddit_id implicitly scopes the result), but set to True/False if
        you want a sanity check that posts match the sub's flag."""
        if brand_id is not None:
            query = """SELECT DISTINCT p.* FROM posts p
                       JOIN post_brands pb ON pb.post_id = p.id
                       WHERE p.subreddit_id = ? AND pb.brand_id = ?"""
            params = [subreddit_id, brand_id]
        else:
            query = "SELECT * FROM posts p WHERE p.subreddit_id = ?"
            params = [subreddit_id]
        if not include_filler:
            query += " AND p.is_filler = 0"
        if live is False:
            query += " AND COALESCE(p.is_live, 0) = 0"
        elif live is True:
            query += " AND COALESCE(p.is_live, 0) = 1"
        query += " ORDER BY p.suggested_post_day ASC, p.created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_posts_with_details(self, subreddit_id, brand_id=None, limit=200, include_filler=True, date=None):
        """Get posts with comment counts, reddit_url, and brand names in a single query."""
        comment_counts = """
                       COUNT(DISTINCT CASE WHEN c.status != 'deleted' THEN c.id END) as total_comments,
                       COUNT(DISTINCT CASE WHEN c.status = 'deployed' THEN c.id END) as comment_count,
                       COUNT(DISTINCT CASE WHEN c.status IN ('assigned','informed','deployed','paid') AND c.mentions_brand = 1 THEN c.id END) as assigned_brand,
                       COUNT(DISTINCT CASE WHEN c.status = 'deployed' AND c.mentions_brand = 1 THEN c.id END) as deployed_brand,
                       COUNT(DISTINCT CASE WHEN c.status IN ('assigned','informed','deployed','paid') AND (c.mentions_brand = 0 OR c.mentions_brand IS NULL) THEN c.id END) as assigned_non_brand,
                       COUNT(DISTINCT CASE WHEN c.status = 'deployed' AND (c.mentions_brand = 0 OR c.mentions_brand IS NULL) THEN c.id END) as deployed_non_brand,"""
        if brand_id is not None:
            query = f"""SELECT p.*,{comment_counts}
                       pu.reddit_url,
                       GROUP_CONCAT(DISTINCT b2.name) as brand_names,
                       GROUP_CONCAT(DISTINCT b2.id || ':' || b2.name) as brand_info
                FROM posts p
                JOIN post_brands pb_filter ON pb_filter.post_id = p.id AND pb_filter.brand_id = ?
                LEFT JOIN comments c ON c.post_id = p.id
                LEFT JOIN post_urls pu ON pu.post_id = p.id
                LEFT JOIN post_brands pb ON pb.post_id = p.id
                LEFT JOIN brands b2 ON b2.id = pb.brand_id
                WHERE p.subreddit_id = ?"""
            params = [brand_id, subreddit_id]
        else:
            query = f"""SELECT p.*,{comment_counts}
                       pu.reddit_url,
                       GROUP_CONCAT(DISTINCT b2.name) as brand_names,
                       GROUP_CONCAT(DISTINCT b2.id || ':' || b2.name) as brand_info
                FROM posts p
                LEFT JOIN comments c ON c.post_id = p.id
                LEFT JOIN post_urls pu ON pu.post_id = p.id
                LEFT JOIN post_brands pb ON pb.post_id = p.id
                LEFT JOIN brands b2 ON b2.id = pb.brand_id
                WHERE p.subreddit_id = ?"""
            params = [subreddit_id]
        if not include_filler:
            query += " AND p.is_filler = 0"
        if date:
            query += " AND DATE(COALESCE(p.deployed_at, p.created_at)) = ?"
            params.append(date)
        query += " GROUP BY p.id ORDER BY p.suggested_post_day ASC, p.created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        results = []
        for r in rows:
            p = dict(r)
            # Parse brand_info into brands list
            brand_info = p.pop("brand_info", None)
            if brand_info:
                brands = []
                for item in brand_info.split(","):
                    parts = item.split(":", 1)
                    if len(parts) == 2:
                        brands.append({"id": int(parts[0]), "name": parts[1]})
                p["brands"] = brands
            else:
                p["brands"] = []
            # Junction empty, but the post's brand is recoverable elsewhere —
            # surface it so brand lookups (HQ / comment generation) and the UI's
            # Brands column resolve it instead of showing "no brand assigned":
            #   1) posts.brand_id (reported / manually-added posts)
            #   2) the post's EXISTING comments' brand_id — an already-generated
            #      HQ cluster stamps brand_id on its comments even when the post
            #      row has no brand link. This is the "+HQ on an existing
            #      cluster" case that kept failing.
            if not p["brands"]:
                fb = None
                if p.get("brand_id"):
                    fb = self.conn.execute(
                        "SELECT id, name FROM brands WHERE id = ?", (p["brand_id"],)
                    ).fetchone()
                if not fb:
                    fb = self.conn.execute(
                        "SELECT b.id AS id, b.name AS name FROM comments c "
                        "JOIN brands b ON b.id = c.brand_id "
                        "WHERE c.post_id = ? AND c.brand_id IS NOT NULL "
                        "ORDER BY c.id LIMIT 1",
                        (p["id"],)
                    ).fetchone()
                if fb:
                    p["brands"] = [{"id": fb["id"], "name": fb["name"]}]
                    if not p.get("brand_names"):
                        p["brand_names"] = fb["name"]
            if not p.get("brand_names"):
                p["brand_names"] = ""
            if not p.get("reddit_url"):
                p["reddit_url"] = ""
            results.append(p)
        return results

    def get_all_posts(self, brand_id=None, subreddit_id=None, status=None, date=None, limit=200, live=False):
        """Get all posts across all subreddits with comment counts.

        `live` controls Live Subreddits filtering: False (default) excludes
        live posts, True returns only live posts, None returns both.
        """
        comment_counts = """
                       COUNT(DISTINCT CASE WHEN c.status != 'deleted' THEN c.id END) as total_comments,
                       COUNT(DISTINCT CASE WHEN c.status = 'deployed' THEN c.id END) as comment_count,
                       COUNT(DISTINCT CASE WHEN c.status IN ('assigned','informed','deployed','paid') AND c.mentions_brand = 1 THEN c.id END) as assigned_brand,
                       COUNT(DISTINCT CASE WHEN c.status = 'deployed' AND c.mentions_brand = 1 THEN c.id END) as deployed_brand,
                       COUNT(DISTINCT CASE WHEN c.status IN ('assigned','informed','deployed','paid') AND (c.mentions_brand = 0 OR c.mentions_brand IS NULL) THEN c.id END) as assigned_non_brand,
                       COUNT(DISTINCT CASE WHEN c.status = 'deployed' AND (c.mentions_brand = 0 OR c.mentions_brand IS NULL) THEN c.id END) as deployed_non_brand,"""
        query = f"""SELECT p.*,{comment_counts}
                       pu.reddit_url,
                       s.name as subreddit_name,
                       GROUP_CONCAT(DISTINCT b2.name) as brand_names,
                       GROUP_CONCAT(DISTINCT b2.id || ':' || b2.name) as brand_info
                FROM posts p
                JOIN subreddits s ON s.id = p.subreddit_id
                LEFT JOIN comments c ON c.post_id = p.id
                LEFT JOIN post_urls pu ON pu.post_id = p.id
                LEFT JOIN post_brands pb ON pb.post_id = p.id
                LEFT JOIN brands b2 ON b2.id = pb.brand_id
                WHERE 1=1"""
        params = []
        if brand_id:
            query += " AND p.id IN (SELECT post_id FROM post_brands WHERE brand_id = ?)"
            params.append(brand_id)
        if subreddit_id:
            query += " AND p.subreddit_id = ?"
            params.append(subreddit_id)
        if status:
            query += " AND p.status = ?"
            params.append(status)
        if date:
            query += " AND DATE(COALESCE(p.deployed_at, p.created_at)) = ?"
            params.append(date)
        if live is False:
            query += " AND COALESCE(p.is_live, 0) = 0"
        elif live is True:
            query += " AND COALESCE(p.is_live, 0) = 1"
        query += " GROUP BY p.id ORDER BY p.created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        results = []
        for r in rows:
            p = dict(r)
            brand_info = p.pop("brand_info", None)
            if brand_info:
                brands = []
                for item in brand_info.split(","):
                    parts = item.split(":", 1)
                    if len(parts) == 2:
                        brands.append({"id": int(parts[0]), "name": parts[1]})
                p["brands"] = brands
            else:
                p["brands"] = []
            # Junction empty, but the post's brand is recoverable elsewhere —
            # surface it so brand lookups (HQ / comment generation) and the UI's
            # Brands column resolve it instead of showing "no brand assigned":
            #   1) posts.brand_id (reported / manually-added posts)
            #   2) the post's EXISTING comments' brand_id — an already-generated
            #      HQ cluster stamps brand_id on its comments even when the post
            #      row has no brand link. This is the "+HQ on an existing
            #      cluster" case that kept failing.
            if not p["brands"]:
                fb = None
                if p.get("brand_id"):
                    fb = self.conn.execute(
                        "SELECT id, name FROM brands WHERE id = ?", (p["brand_id"],)
                    ).fetchone()
                if not fb:
                    fb = self.conn.execute(
                        "SELECT b.id AS id, b.name AS name FROM comments c "
                        "JOIN brands b ON b.id = c.brand_id "
                        "WHERE c.post_id = ? AND c.brand_id IS NOT NULL "
                        "ORDER BY c.id LIMIT 1",
                        (p["id"],)
                    ).fetchone()
                if fb:
                    p["brands"] = [{"id": fb["id"], "name": fb["name"]}]
                    if not p.get("brand_names"):
                        p["brand_names"] = fb["name"]
            if not p.get("brand_names"):
                p["brand_names"] = ""
            if not p.get("reddit_url"):
                p["reddit_url"] = ""
            results.append(p)
        return results

    def get_post(self, post_id):
        row = self.conn.execute(
            "SELECT p.*, (SELECT pu.reddit_url FROM post_urls pu WHERE pu.post_id = p.id LIMIT 1) "
            "AS reddit_url FROM posts p WHERE p.id = ?",
            (post_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_post_status(self, post_id, status):
        self.conn.execute("UPDATE posts SET status = ? WHERE id = ?", (status, post_id))
        self.conn.commit()

    def update_post_body(self, post_id, body):
        """Replace a post's body (used by the 'Regenerate body' action — title kept)."""
        self.conn.execute("UPDATE posts SET body = ? WHERE id = ?", (body or "", post_id))
        self.conn.commit()

    def undeploy_post(self, post_id):
        """Revert a deployed (published) post back to complete status.

        Also clears the linked Reddit URL — leaving it would mean a later
        redeploy without a fresh URL would still report the old link, and
        any "View" buttons would point at the wrong reddit thread.
        """
        self.conn.execute(
            """UPDATE posts SET status = 'complete', deployed_at = NULL, paid_at = NULL,
                   report_month = NULL, report_added_at = NULL, prev_status = NULL
               WHERE id = ? AND status IN ('published', 'paid')""",
            (post_id,)
        )
        self.conn.execute("DELETE FROM post_urls WHERE post_id = ?", (post_id,))
        self.conn.commit()

    def delete_subreddit(self, subreddit_id):
        """Delete a subreddit and all its underlying data (brands, posts, comments, post_urls)."""
        # Get all post IDs for this subreddit
        post_ids = [r[0] for r in self.conn.execute(
            "SELECT id FROM posts WHERE subreddit_id = ?", (subreddit_id,)
        ).fetchall()]
        if post_ids:
            placeholders = ",".join("?" * len(post_ids))
            # Adjust lifetime counters before deleting comments
            comment_accounts = self.conn.execute(
                f"""SELECT account_id, COUNT(*) AS cnt FROM comments
                    WHERE post_id IN ({placeholders}) AND account_id IS NOT NULL
                      AND status NOT IN ('draft','complete')
                    GROUP BY account_id""",
                post_ids
            ).fetchall()
            for r in comment_accounts:
                self._decrement_lifetime(r["account_id"], r["cnt"])
            # Adjust lifetime counters for post owners
            post_owners = self.conn.execute(
                f"""SELECT owner_account, COUNT(*) AS cnt FROM posts
                    WHERE id IN ({placeholders}) AND owner_account IS NOT NULL AND owner_account != ''
                    GROUP BY owner_account""",
                post_ids
            ).fetchall()
            for r in post_owners:
                self._decrement_lifetime(r["owner_account"], r["cnt"])
            self.conn.execute(f"DELETE FROM comments WHERE post_id IN ({placeholders})", post_ids)
            self.conn.execute(f"DELETE FROM post_urls WHERE post_id IN ({placeholders})", post_ids)
            self.conn.execute(f"DELETE FROM post_brands WHERE post_id IN ({placeholders})", post_ids)
            self.conn.execute(f"DELETE FROM posts WHERE subreddit_id = ?", (subreddit_id,))
        # Delete post_urls linked by subreddit_id (those without post_id)
        self.conn.execute("DELETE FROM post_urls WHERE subreddit_id = ?", (subreddit_id,))

        # NULL out brand_id references in search tables before deleting brands
        brand_ids = [r[0] for r in self.conn.execute(
            "SELECT id FROM brands WHERE subreddit_id = ?", (subreddit_id,)
        ).fetchall()]
        if brand_ids:
            ph = ",".join("?" * len(brand_ids))
            # Guard: search tables may not exist in older databases
            for tbl in ("search_comments", "search_posts"):
                if self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
                ).fetchone():
                    self.conn.execute(
                        f"UPDATE {tbl} SET brand_id = NULL WHERE brand_id IN ({ph})", brand_ids
                    )

        self.conn.execute("DELETE FROM brands WHERE subreddit_id = ?", (subreddit_id,))
        self.conn.execute("DELETE FROM subreddits WHERE id = ?", (subreddit_id,))
        self.conn.commit()

    def delete_post(self, post_id):
        # Free rotation slots: (a) owner_account of the post itself, (b) every
        # child comment that still had an account_id.
        owner_row = self.conn.execute(
            "SELECT owner_account FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        if owner_row and owner_row["owner_account"]:
            self._decrement_lifetime(owner_row["owner_account"])
        comment_rows = self.conn.execute(
            """SELECT account_id, COUNT(*) AS cnt FROM comments
               WHERE post_id = ? AND account_id IS NOT NULL AND status != 'draft'
               GROUP BY account_id""",
            (post_id,)
        ).fetchall()
        for r in comment_rows:
            self._decrement_lifetime(r["account_id"], r["cnt"])
        self.conn.execute("DELETE FROM comments WHERE post_id = ?", (post_id,))
        self.conn.execute("DELETE FROM post_urls WHERE post_id = ?", (post_id,))
        self.conn.execute("DELETE FROM post_brands WHERE post_id = ?", (post_id,))
        self.conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        self.conn.commit()

    def get_storyline_distribution(self, subreddit_id, brand_id=None):
        if brand_id is not None:
            query = """SELECT p.storyline, COUNT(*) as cnt FROM posts p
                       JOIN post_brands pb ON pb.post_id = p.id
                       WHERE p.subreddit_id = ? AND pb.brand_id = ?
                       GROUP BY p.storyline"""
            params = [subreddit_id, brand_id]
        else:
            query = "SELECT storyline, COUNT(*) as cnt FROM posts WHERE subreddit_id = ? GROUP BY storyline"
            params = [subreddit_id]
        rows = self.conn.execute(query, params).fetchall()
        return {r["storyline"]: r["cnt"] for r in rows}

    def get_all_post_titles_for_brand(self, brand_name):
        """Returns all post titles across ALL subreddits for any brand with this name.

        NOTE: Used for analytics/aggregate counts only. For title-dedup
        during post generation, use `get_post_titles_for_brand_in_subreddit`
        — we intentionally ALLOW the same title to be reused across
        different subreddits.
        """
        rows = self.conn.execute(
            """SELECT DISTINCT p.title FROM posts p
               JOIN post_brands pb ON pb.post_id = p.id
               JOIN brands b ON pb.brand_id = b.id
               WHERE LOWER(b.name) = LOWER(?)""",
            (brand_name,)
        ).fetchall()
        return [r["title"] for r in rows]

    def get_post_titles_for_brand_in_subreddit(self, brand_name, subreddit_id):
        """Titles already used by this brand WITHIN a single subreddit.

        Drives title dedup during post generation. Scoping by subreddit
        means the same title can be reused in a different subreddit —
        which is what we want, since cross-sub reposts are valid
        strategy. Within the same subreddit we still block duplicates
        because Reddit rejects exact-title reposts in many subs.
        """
        if subreddit_id is None:
            return self.get_all_post_titles_for_brand(brand_name)
        rows = self.conn.execute(
            """SELECT DISTINCT p.title FROM posts p
               JOIN post_brands pb ON pb.post_id = p.id
               JOIN brands b ON pb.brand_id = b.id
               WHERE LOWER(b.name) = LOWER(?)
                 AND p.subreddit_id = ?""",
            (brand_name, subreddit_id)
        ).fetchall()
        return [r["title"] for r in rows]

    # --- AI-Search cluster persistence (gap-fill / completion) ---

    @staticmethod
    def normalize_seed(seed):
        return (seed or "").strip().lower()

    @staticmethod
    def _norm_query(s):
        """Normalize a query/title for tolerant matching: lowercase, strip punctuation
        to spaces, collapse whitespace. So 'Best longevity clinic for men?' and
        'best longevity clinic for men' compare equal."""
        s = (s or "").lower()
        s = re.sub(r"[^a-z0-9]+", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    # Small, domain-neutral stopword set so content words (longevity, clinic, hormone…)
    # drive matching, not filler (how/do/for/with…).
    _MATCH_STOPWORDS = frozenset((
        "a an and or the of for to in on at is are be do does did how what which who "
        "whom whose why when where with without my me i you your they them it its as by "
        "from into over under about best vs versus v can could should would will than "
        "that this these those any some get got using use").split())

    @classmethod
    def _content_tokens(cls, s):
        return {t for t in cls._norm_query(s).split() if t and t not in cls._MATCH_STOPWORDS}

    @classmethod
    def match_query_to_rewrites(cls, target_query, rewrite_queries):
        """Map a post's (often LLM-paraphrased) target_query to the canonical cluster
        rewrite it best corresponds to. Returns the matching rewrite query string (the
        ORIGINAL casing from rewrite_queries) or None.

        Strategy — tolerant of paraphrase, conservative against mis-binding:
          1. normalized exact match (case/punctuation-insensitive);
          2. else compare CONTENT tokens (stopwords stripped). Require >= 2 shared
             content tokens (so a single common word like "longevity" can't create
             false coverage), then score = max(Jaccard, overlap-coefficient) and take
             the best rewrite scoring >= 0.5. Ties → first in list.
        """
        tq = cls._norm_query(target_query)
        if not tq or not rewrite_queries:
            return None
        norm_pairs = [(rq, cls._norm_query(rq)) for rq in rewrite_queries]
        for rq, nrq in norm_pairs:
            if nrq and nrq == tq:
                return rq
        tq_c = cls._content_tokens(target_query)
        if not tq_c:
            return None
        best, best_score = None, 0.0
        for rq, _ in norm_pairs:
            rc = cls._content_tokens(rq)
            if not rc:
                continue
            shared = len(tq_c & rc)
            if shared < 2:
                continue
            jac = shared / len(tq_c | rc)
            overlap = shared / min(len(tq_c), len(rc))
            score = max(jac, overlap)
            if score > best_score:
                best, best_score = rq, score
        return best if best_score >= 0.5 else None

    def get_ai_search_cluster(self, brand_id, seed_norm):
        """Return the persisted fan-out cluster row for (brand, seed) or None."""
        row = self.conn.execute(
            "SELECT * FROM ai_search_clusters WHERE brand_id IS ? AND seed_norm = ?",
            (brand_id, seed_norm)
        ).fetchone()
        return dict(row) if row else None

    def save_ai_search_cluster(self, brand_id, seed_norm, seed, anchor, rewrites, checklist):
        """Persist the canonical cluster for (brand, seed) once (idempotent)."""
        self.conn.execute(
            """INSERT OR IGNORE INTO ai_search_clusters
               (brand_id, seed_norm, seed, anchor, rewrites_json, checklist_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (brand_id, seed_norm, seed or "", anchor or "",
             json.dumps(rewrites or []), json.dumps(checklist or []))
        )
        self.conn.commit()
        return self.get_ai_search_cluster(brand_id, seed_norm)

    def upsert_ai_search_cluster(self, brand_id, seed_norm, seed, anchor, rewrites,
                                 checklist, backfilled=0):
        """Insert OR update the cluster for (brand, seed) — used to (re)build/expand
        it (e.g. fold past posts in + extend on re-generation, or upgrade a
        backfilled cluster). Overwrites rewrites/checklist/anchor/seed/backfilled."""
        existing = self.get_ai_search_cluster(brand_id, seed_norm)
        if existing:
            self.conn.execute(
                """UPDATE ai_search_clusters
                   SET seed=?, anchor=?, rewrites_json=?, checklist_json=?, backfilled=?
                   WHERE brand_id IS ? AND seed_norm=?""",
                (seed or "", anchor or "", json.dumps(rewrites or []),
                 json.dumps(checklist or []), int(backfilled), brand_id, seed_norm))
        else:
            self.conn.execute(
                """INSERT INTO ai_search_clusters
                   (brand_id, seed_norm, seed, anchor, rewrites_json, checklist_json, backfilled)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (brand_id, seed_norm, seed or "", anchor or "",
                 json.dumps(rewrites or []), json.dumps(checklist or []), int(backfilled)))
        self.conn.commit()
        return self.get_ai_search_cluster(brand_id, seed_norm)

    def get_ai_search_clusters_for_brand(self, brand_id=None):
        """All persisted clusters (a brand's root prompts). brand_id None → all."""
        if brand_id is None:
            rows = self.conn.execute(
                "SELECT * FROM ai_search_clusters ORDER BY created_at DESC, id DESC"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM ai_search_clusters WHERE brand_id IS ? "
                "ORDER BY created_at DESC, id DESC", (brand_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def normalize_rewrites(raw):
        """A cluster's rewrites_json is either a legacy flat [str] or the
        [{query, region, source, variants, persona}] form. Return the object form
        (backward-compatible), deduped by query (case-insensitive).

        `variants` = the real phrasings that map to this region (body-side retrieval
        context); `persona` = the persona this region most represents (attribution).
        Legacy rows → variants []/persona "".
        """
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                raw = []
        out, seen = [], set()
        for r in (raw or []):
            variants, persona = [], ""
            if isinstance(r, dict):
                q = (r.get("query") or "").strip()
                region = r.get("region") or "(unsorted)"
                source = r.get("source") or "generated"
                persona = (r.get("persona") or "").strip()
                _vseen = set()
                for v in (r.get("variants") or []):
                    vs = str(v).strip()
                    if vs and vs.lower() not in _vseen:
                        _vseen.add(vs.lower())
                        variants.append(vs)
            else:
                q = str(r).strip()
                region, source = "(unsorted)", "generated"
            k = q.lower()
            if q and k not in seen:
                seen.add(k)
                out.append({"query": q, "region": region, "source": source,
                            "variants": variants, "persona": persona})
        return out

    # --- Blogs (GEO articles + per-platform publish tracking) ---

    def save_blog(self, brand_id, seed, title="", meta_description="", keywords=None,
                  body_markdown="", linkedin_text="", claims_flagged=None,
                  status="draft", prompt_version="", source_urls=None, research_notes="",
                  use_web_search=0):
        """Insert a blog row. keywords/claims_flagged/source_urls are stored as JSON. Returns id."""
        cur = self.conn.execute(
            """INSERT INTO blogs (brand_id, seed, title, meta_description, keywords,
                                  body_markdown, linkedin_text, claims_flagged, source_urls,
                                  research_notes, use_web_search, status, prompt_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (brand_id, seed, title, meta_description,
             json.dumps(keywords or []),
             body_markdown, linkedin_text,
             json.dumps(claims_flagged or []),
             json.dumps(source_urls or []),
             research_notes or "",
             1 if use_web_search else 0,
             status, prompt_version),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_blog(self, blog_id):
        """One blog with brand name + its platform rows. None if missing."""
        row = self.conn.execute(
            """SELECT b.*, br.name AS brand_name
               FROM blogs b LEFT JOIN brands br ON br.id = b.brand_id
               WHERE b.id = ?""", (blog_id,)
        ).fetchone()
        if not row:
            return None
        blog = dict(row)
        try:
            blog["keywords"] = json.loads(blog.get("keywords") or "[]")
        except (json.JSONDecodeError, TypeError):
            blog["keywords"] = []
        try:
            blog["claims_flagged"] = json.loads(blog.get("claims_flagged") or "[]")
        except (json.JSONDecodeError, TypeError):
            blog["claims_flagged"] = []
        try:
            blog["source_urls"] = json.loads(blog.get("source_urls") or "[]")
        except (json.JSONDecodeError, TypeError):
            blog["source_urls"] = []
        prows = self.conn.execute(
            "SELECT platform, published_url, published_at, status FROM blog_platforms "
            "WHERE blog_id = ? ORDER BY platform", (blog_id,)
        ).fetchall()
        blog["platforms"] = [dict(p) for p in prows]
        return blog

    def get_all_blogs(self, brand_id=None, status=None):
        """List blogs (newest first) + a compact platforms summary, with optional
        brand/status filters."""
        q = ("SELECT b.id, b.brand_id, b.seed, b.title, b.status, b.created_at, "
             "b.updated_at, br.name AS brand_name "
             "FROM blogs b LEFT JOIN brands br ON br.id = b.brand_id WHERE 1=1")
        params = []
        if brand_id is not None:
            q += " AND b.brand_id IS ?"; params.append(brand_id)
        if status:
            q += " AND b.status = ?"; params.append(status)
        q += " ORDER BY b.created_at DESC, b.id DESC"
        rows = [dict(r) for r in self.conn.execute(q, params).fetchall()]
        if rows:
            ids = [r["id"] for r in rows]
            ph = ",".join("?" * len(ids))
            prows = self.conn.execute(
                f"SELECT blog_id, platform, published_url, status FROM blog_platforms "
                f"WHERE blog_id IN ({ph})", ids
            ).fetchall()
            by_blog = {}
            for p in prows:
                by_blog.setdefault(p["blog_id"], []).append(dict(p))
            for r in rows:
                r["platforms"] = by_blog.get(r["id"], [])
        return rows

    def update_blog(self, blog_id, **fields):
        """Patch blog columns. keywords/claims_flagged are JSON-encoded if passed as
        list/dict. Always bumps updated_at. No-op when no known fields are given."""
        allowed = {"seed", "title", "meta_description", "keywords", "body_markdown",
                   "linkedin_text", "claims_flagged", "status", "prompt_version",
                   "source_urls", "research_notes", "use_web_search"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("keywords", "claims_flagged", "source_urls") and not isinstance(v, str):
                v = json.dumps(v or [])
            sets.append(f"{k} = ?")
            params.append(v)
        if not sets:
            return
        sets.append("updated_at = datetime('now')")
        params.append(blog_id)
        self.conn.execute(f"UPDATE blogs SET {', '.join(sets)} WHERE id = ?", params)
        self.conn.commit()

    def update_blog_status(self, blog_id, status):
        self.conn.execute(
            "UPDATE blogs SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, blog_id))
        self.conn.commit()

    def delete_blog(self, blog_id):
        """Delete a blog; blog_platforms cascade via FK (PRAGMA foreign_keys = ON).
        Belt-and-suspenders: clear platform rows first in case the cascade is off."""
        self.conn.execute("DELETE FROM blog_platforms WHERE blog_id = ?", (blog_id,))
        cur = self.conn.execute("DELETE FROM blogs WHERE id = ?", (blog_id,))
        self.conn.commit()
        return cur.rowcount

    def _roll_blog_status(self, blog_id):
        """Set blogs.status to 'published' when >=1 platform is published, else back to
        'draft' (unless the blog was archived). Keeps the rollup honest."""
        cur = self.conn.execute(
            "SELECT status FROM blogs WHERE id = ?", (blog_id,)).fetchone()
        if not cur or cur["status"] == "archived":
            return
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM blog_platforms "
            "WHERE blog_id = ? AND status = 'published'", (blog_id,)).fetchone()["n"]
        self.update_blog_status(blog_id, "published" if n else "draft")

    def upsert_blog_platform(self, blog_id, platform, published_url=None,
                             status="published"):
        """Mark a blog published (or update its live URL) on ONE platform. UNIQUE
        (blog_id, platform) → insert-or-update, never duplicate. Rolls blog.status."""
        self.conn.execute(
            """INSERT INTO blog_platforms (blog_id, platform, published_url, published_at, status)
               VALUES (?, ?, ?, datetime('now'), ?)
               ON CONFLICT(blog_id, platform) DO UPDATE SET
                   published_url = excluded.published_url,
                   published_at  = excluded.published_at,
                   status        = excluded.status""",
            (blog_id, platform, published_url, status))
        self.conn.commit()
        self._roll_blog_status(blog_id)

    def unpublish_blog_platform(self, blog_id, platform):
        """Remove a platform's published mark. Rolls blog.status back if needed."""
        self.conn.execute(
            "DELETE FROM blog_platforms WHERE blog_id = ? AND platform = ?",
            (blog_id, platform))
        self.conn.commit()
        self._roll_blog_status(blog_id)

    def delete_ai_search_cluster(self, brand_id, seed_norm):
        """Remove a cluster row (posts are kept). Returns rows deleted."""
        cur = self.conn.execute(
            "DELETE FROM ai_search_clusters WHERE brand_id IS ? AND seed_norm = ?",
            (brand_id, seed_norm))
        self.conn.commit()
        return cur.rowcount

    def get_ai_search_posts_for_seed(self, brand_id, seed_norm):
        """Posts generated under a given (brand, seed) root, with their target_query
        and display fields — for the Clusters coverage view."""
        rows = self.conn.execute(
            """SELECT p.id, p.title, p.status, p.post_number, p.ai_search_meta,
                      s.name AS subreddit_name
               FROM posts p LEFT JOIN subreddits s ON s.id = p.subreddit_id
               WHERE p.brand_id IS ? AND p.ai_search_meta IS NOT NULL""",
            (brand_id,)
        ).fetchall()
        out = []
        for r in rows:
            try:
                meta = json.loads(r["ai_search_meta"])
            except (json.JSONDecodeError, TypeError):
                continue
            if self.normalize_seed(meta.get("seed")) != seed_norm:
                continue
            out.append({
                "id": r["id"], "title": r["title"], "status": r["status"],
                "post_number": r["post_number"], "subreddit_name": r["subreddit_name"],
                "target_query": (meta.get("target_query") or "").strip(),
                # Stable region identity stamped at production time (coverage joins on
                # this); empty for legacy posts that predate the stamp.
                "region": (meta.get("region") or "").strip(),
            })
        return out

    def backfill_clusters_from_posts(self, brand_id=None):
        """Reconstruct ai_search_clusters rows from previously-generated AI-Search
        posts (their ai_search_meta), for (brand, seed) groups that don't have a
        cluster yet. rewrites = the distinct target_queries already covered;
        marked backfilled=1 (the original fan-out's never-generated gaps can't be
        recovered). Returns the number of clusters created."""
        if brand_id is None:
            rows = self.conn.execute(
                "SELECT brand_id, ai_search_meta FROM posts WHERE ai_search_meta IS NOT NULL"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT brand_id, ai_search_meta FROM posts "
                "WHERE brand_id IS ? AND ai_search_meta IS NOT NULL", (brand_id,)
            ).fetchall()
        groups = {}  # (brand_id, seed_norm) -> {seed, anchor, targets:[...]}
        for r in rows:
            try:
                meta = json.loads(r["ai_search_meta"])
            except (json.JSONDecodeError, TypeError):
                continue
            seed = (meta.get("seed") or "").strip()
            sn = self.normalize_seed(seed)
            if not sn:
                continue
            tq = (meta.get("target_query") or "").strip()
            g = groups.setdefault((r["brand_id"], sn),
                                  {"seed": seed, "anchor": meta.get("anchor") or "", "targets": []})
            if tq and tq.lower() not in [t.lower() for t in g["targets"]]:
                g["targets"].append(tq)
        n = 0
        for (bid, sn), g in groups.items():
            if self.get_ai_search_cluster(bid, sn) or not g["targets"]:
                continue
            rw_objs = [{"query": t, "region": "(from posts)", "source": "manual"} for t in g["targets"]]
            self.conn.execute(
                """INSERT OR IGNORE INTO ai_search_clusters
                   (brand_id, seed_norm, seed, anchor, rewrites_json, checklist_json, backfilled)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (bid, sn, g["seed"], g["anchor"], json.dumps(rw_objs), json.dumps([]))
            )
            n += 1
        self.conn.commit()
        return n

    def attach_posts_to_cluster(self, brand_id, seed_norm, post_numbers, region_by_num=None):
        """Manually attach existing posts (by their per-brand post_number) to a cluster.

        `region_by_num` (optional {post_number: region}) — the caller classifies each
        post's title into a region first (via post_gen._classify_regions). When given:
          - region matches an EXISTING cluster region → the post COVERS it: its
            target_query is set to that region's primary query, and the post's title is
            added to that region's `variants` (a real phrasing);
          - else → a NEW rewrite is added with the classified region (or "(from posts)").
        Without `region_by_num`, falls back to the old behavior ("(from posts)").
        Returns {attached:[nums], not_found:[nums], existing:[nums], new:[nums], cluster_size:N}."""
        cluster = self.get_ai_search_cluster(brand_id, seed_norm)
        if not cluster:
            return {"error": "no cluster for this seed", "attached": [], "not_found": []}
        rewrites = self.normalize_rewrites(cluster.get("rewrites_json"))
        region_by_num = region_by_num or {}
        lower = {r["query"].strip().lower(): r for r in rewrites}
        by_region = {}
        for r in rewrites:
            reg = (r.get("region") or "").strip()
            if reg and reg.lower() not in ("(unsorted)", "(from posts)"):
                by_region.setdefault(reg.lower(), r)
        seed = cluster.get("seed") or seed_norm
        anchor = cluster.get("anchor") or ""
        attached, not_found, into_existing, into_new = [], [], [], []
        for num in post_numbers:
            row = self.conn.execute(
                "SELECT id, title, ai_search_meta FROM posts "
                "WHERE brand_id IS ? AND post_number = ?", (brand_id, num)
            ).fetchone()
            if not row:
                not_found.append(num)
                continue
            try:
                meta = json.loads(row["ai_search_meta"]) if row["ai_search_meta"] else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            title = (row["title"] or "").strip()[:120]
            region = (region_by_num.get(num) or "").strip()
            if region and region.lower() in by_region:
                # Post covers an existing region → target that region's query + keep
                # the post's title as a variant (a real phrasing).
                kept = by_region[region.lower()]
                tq = kept["query"]
                meta_region = kept.get("region") or region
                vs = kept.setdefault("variants", [])
                if title and title.lower() != tq.lower() and title.lower() not in {v.lower() for v in vs}:
                    vs.append(title)
                into_existing.append(num)
            else:
                tq = (meta.get("target_query") or "").strip() or title
                reg = region or "(from posts)"
                meta_region = reg
                if tq and tq.lower() not in lower:
                    new_rw = {"query": tq, "region": reg, "source": "manual", "variants": [], "persona": ""}
                    rewrites.append(new_rw)
                    lower[tq.lower()] = new_rw
                    if reg.lower() not in ("(unsorted)", "(from posts)"):
                        by_region.setdefault(reg.lower(), new_rw)
                into_new.append(num)
            # Stamp the covered region into ai_search_meta so the coverage view joins on
            # the stable region identity (not re-matched text).
            self.conn.execute(
                "UPDATE posts SET ai_search_meta=? WHERE id=?",
                (json.dumps({"seed": seed, "anchor": anchor, "target_query": tq,
                             "persona": meta.get("persona", ""), "region": meta_region}), row["id"]))
            attached.append(num)
        self.conn.commit()
        self.upsert_ai_search_cluster(
            brand_id, seed_norm, seed, anchor, rewrites,
            json.loads(cluster.get("checklist_json") or "[]"), backfilled=0)
        return {"attached": attached, "not_found": not_found,
                "existing": into_existing, "new": into_new, "cluster_size": len(rewrites)}

    def get_covered_target_queries(self, brand_id, seed_norm):
        """Set of normalized target_query strings already covered (= a post was
        generated) for this (brand, seed). Parses posts.ai_search_meta JSON."""
        rows = self.conn.execute(
            "SELECT ai_search_meta FROM posts "
            "WHERE brand_id IS ? AND ai_search_meta IS NOT NULL",
            (brand_id,)
        ).fetchall()
        covered = set()
        for r in rows:
            try:
                meta = json.loads(r["ai_search_meta"])
            except (json.JSONDecodeError, TypeError):
                continue
            if self.normalize_seed(meta.get("seed")) != seed_norm:
                continue
            tq = (meta.get("target_query") or "").strip().lower()
            if tq:
                covered.add(tq)
        return covered

    # --- Comments ---

    def save_comment(self, post_id, brand_id, body, persona_id=None,
                     structure_id=None, is_reply=0, parent_comment_id=None,
                     mentions_brand=0, validation_score=None, account_id=None,
                     status="draft", suggested_post_day=0, suggested_order=0,
                     prompt_version=None, comment_type="",
                     focus_phrase=None, focus_hit=None):
        cur = self.conn.execute(
            """INSERT INTO comments (post_id, brand_id, body, persona_id, structure_id,
               is_reply, parent_comment_id, mentions_brand, validation_score, account_id,
               status, suggested_post_day, suggested_order, prompt_version, comment_type,
               focus_phrase, focus_hit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (post_id, brand_id, body, persona_id, structure_id,
             is_reply, parent_comment_id, mentions_brand, validation_score, account_id,
             status, suggested_post_day, suggested_order, prompt_version, comment_type,
             focus_phrase, focus_hit)
        )
        self.conn.commit()
        return cur.lastrowid

    def get_comments(self, post_id):
        rows = self.conn.execute(
            "SELECT * FROM comments WHERE post_id = ? ORDER BY suggested_post_day, suggested_order, id",
            (post_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_comment_tree(self, post_id):
        """Get comments organized as a recursive tree with parent-child relationships."""
        comments = self.get_comments(post_id)

        # Build lookup maps
        by_id = {}
        reply_map = {}  # parent_id -> list of children
        for c in comments:
            node = dict(c)
            node["replies"] = []
            by_id[node["id"]] = node
            pid = node["parent_comment_id"]
            if pid and node["is_reply"]:
                reply_map.setdefault(pid, []).append(node)

        # Attach children recursively
        def attach(node):
            node["replies"] = reply_map.get(node["id"], [])
            for child in node["replies"]:
                attach(child)

        tree = []
        for c in comments:
            node = by_id[c["id"]]
            if not c["is_reply"] or c["parent_comment_id"] is None:
                attach(node)
                tree.append(node)

        return tree

    def get_comment(self, comment_id):
        row = self.conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
        return dict(row) if row else None

    def update_comment_status(self, comment_id, status):
        self.conn.execute("UPDATE comments SET status = ? WHERE id = ?", (status, comment_id))
        self.conn.commit()

    def update_comment_body(self, comment_id, body):
        self.conn.execute("UPDATE comments SET body = ? WHERE id = ?", (body, comment_id))
        self.conn.commit()

    def delete_comment(self, comment_id):
        """Delete a comment AND all of its descendants (replies, nested replies, …).

        Walks the parent_comment_id tree recursively so HQ thread roots
        cascade-delete the entire cluster. Frees rotation slots for every
        deployed/assigned/etc descendant before deleting.
        """
        # BFS to collect every descendant id under comment_id
        all_ids = [comment_id]
        frontier = [comment_id]
        while frontier:
            placeholders = ",".join("?" * len(frontier))
            kids = self.conn.execute(
                f"SELECT id FROM comments WHERE parent_comment_id IN ({placeholders})",
                frontier
            ).fetchall()
            kid_ids = [r["id"] for r in kids]
            if not kid_ids:
                break
            all_ids.extend(kid_ids)
            frontier = kid_ids

        # Free rotation slots for every row about to be deleted that was
        # actually using a slot (account_id set, status != draft).
        placeholders = ",".join("?" * len(all_ids))
        rows = self.conn.execute(
            f"""SELECT account_id, COUNT(*) AS cnt FROM comments
                WHERE id IN ({placeholders})
                  AND account_id IS NOT NULL
                  AND status != 'draft'
                GROUP BY account_id""",
            all_ids
        ).fetchall()
        for r in rows:
            self._decrement_lifetime(r["account_id"], r["cnt"])

        # Delete leaves-first to keep parent_comment_id FKs clean. Since
        # all_ids is in BFS order (roots first), reverse for leaves-first.
        for cid in reversed(all_ids):
            self.conn.execute("DELETE FROM comments WHERE id = ?", (cid,))
        self.conn.commit()

    def get_all_comment_bodies_for_brand(self, brand_name, limit=200):
        """Returns recent comment bodies across ALL subreddits for this brand."""
        rows = self.conn.execute(
            """SELECT c.body FROM comments c
               JOIN brands b ON c.brand_id = b.id
               WHERE LOWER(b.name) = LOWER(?)
               ORDER BY c.created_at DESC LIMIT ?""",
            (brand_name, limit)
        ).fetchall()
        return [r["body"] for r in rows]

    # --- Analytics Queries ---

    def get_stats_for_subreddit(self, subreddit_id):
        stats = {}
        # Post counts
        row = self.conn.execute(
            """SELECT
                COUNT(*) as total_posts,
                SUM(CASE WHEN is_filler = 1 THEN 1 ELSE 0 END) as filler_posts,
                SUM(CASE WHEN is_filler = 0 THEN 1 ELSE 0 END) as brand_posts,
                SUM(CASE WHEN status IN ('published', 'paid') THEN 1 ELSE 0 END) as published_posts,
                SUM(CASE WHEN status = 'draft' THEN 1 ELSE 0 END) as draft_posts,
                SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) as complete_posts
            FROM posts WHERE subreddit_id = ?""",
            (subreddit_id,)
        ).fetchone()
        stats["posts"] = dict(row)

        # Comment counts
        row = self.conn.execute(
            """SELECT
                COUNT(*) as total_comments,
                SUM(CASE WHEN c.status = 'deployed' THEN 1 ELSE 0 END) as deployed_comments,
                SUM(CASE WHEN c.status = 'deployed' AND c.mentions_brand = 1 THEN 1 ELSE 0 END) as deployed_brand_mentions,
                SUM(CASE WHEN c.status = 'deployed' AND (c.mentions_brand = 0 OR c.mentions_brand IS NULL) THEN 1 ELSE 0 END) as deployed_non_brand,
                SUM(CASE WHEN mentions_brand = 1 THEN 1 ELSE 0 END) as brand_mentions,
                AVG(validation_score) as avg_validation_score
            FROM comments c
            JOIN posts p ON c.post_id = p.id
            WHERE p.subreddit_id = ?""",
            (subreddit_id,)
        ).fetchone()
        stats["comments"] = dict(row)

        return stats

    def get_deployment_analytics(self, subreddit_id=None, brand_id=None, date_from=None, date_to=None):
        """Get deployment stats: totals, per-account, and per-brand breakdown."""
        where_parts = []
        params = []

        if subreddit_id:
            where_parts.append("p.subreddit_id = ?")
            params.append(subreddit_id)
        if brand_id:
            where_parts.append("c.brand_id = ?")
            params.append(brand_id)

        # Date filter on deployed_at for deployed comments
        date_where = []
        if date_from:
            date_where.append("c.deployed_at >= ?")
            params.append(date_from)
        if date_to:
            date_where.append("c.deployed_at <= ?")
            params.append(date_to + " 23:59:59")

        all_where = where_parts + date_where
        where_sql = ("WHERE " + " AND ".join(all_where)) if all_where else ""

        # Totals
        totals = dict(self.conn.execute(f"""
            SELECT
                COUNT(CASE WHEN c.status = 'deployed' THEN 1 END) as deployed_comments,
                COUNT(CASE WHEN c.status = 'deployed' AND c.mentions_brand = 1 THEN 1 END) as deployed_branded,
                COUNT(CASE WHEN c.status = 'deployed' AND c.mentions_brand = 0 THEN 1 END) as deployed_organic,
                COUNT(CASE WHEN c.status IN ('assigned','informed') THEN 1 END) as assigned_comments,
                COUNT(CASE WHEN c.status IN ('draft','complete') THEN 1 END) as pending_comments,
                COUNT(CASE WHEN c.status = 'paid' THEN 1 END) as paid_comments,
                COUNT(CASE WHEN c.status = 'deployed' THEN 1 END) as unpaid_comments
            FROM comments c
            JOIN posts p ON c.post_id = p.id
            {where_sql}
        """, params).fetchone())

        # Post and subreddit counts
        post_where = []
        post_params = []
        if subreddit_id:
            post_where.append("p.subreddit_id = ?")
            post_params.append(subreddit_id)
        if brand_id:
            post_where.append("EXISTS (SELECT 1 FROM post_brands pb WHERE pb.post_id = p.id AND pb.brand_id = ?)")
            post_params.append(brand_id)
        post_where_sql = ("WHERE " + " AND ".join(post_where)) if post_where else ""

        post_row = dict(self.conn.execute(f"""
            SELECT
                COUNT(*) as total_posts,
                COUNT(CASE WHEN p.status IN ('published', 'paid') THEN 1 END) as published_posts,
                COUNT(DISTINCT p.subreddit_id) as subreddit_count
            FROM posts p
            {post_where_sql}
        """, post_params).fetchone())
        totals.update(post_row)

        # Per-account breakdown
        accounts = [dict(r) for r in self.conn.execute(f"""
            SELECT
                c.account_id as username,
                COUNT(*) as total,
                SUM(CASE WHEN c.mentions_brand = 1 THEN 1 ELSE 0 END) as branded,
                SUM(CASE WHEN c.mentions_brand = 0 THEN 1 ELSE 0 END) as organic,
                SUM(CASE WHEN c.status = 'paid' THEN 1 ELSE 0 END) as paid,
                SUM(CASE WHEN c.status = 'deployed' THEN 1 ELSE 0 END) as unpaid
            FROM comments c
            JOIN posts p ON c.post_id = p.id
            {where_sql} {"AND" if where_sql else "WHERE"} c.status IN ('deployed', 'paid') AND c.account_id IS NOT NULL
            GROUP BY c.account_id
            ORDER BY total DESC
        """, params).fetchall()]

        # Per-brand breakdown: subreddits, posts, comments (branded/general) deployed
        bq_parts = ["c.status IN ('deployed', 'paid')"]
        bq_params = []
        if date_from:
            bq_parts.append("c.deployed_at >= ?")
            bq_params.append(date_from)
        if date_to:
            bq_parts.append("c.deployed_at <= ?")
            bq_params.append(date_to + " 23:59:59")
        if subreddit_id:
            bq_parts.append("p.subreddit_id = ?")
            bq_params.append(subreddit_id)
        bq_where = " AND ".join(bq_parts)

        brand_stats = [dict(r) for r in self.conn.execute(f"""
            SELECT
                b.id as brand_id,
                b.name as brand_name,
                COUNT(DISTINCT s.id) as subreddits,
                COUNT(DISTINCT p.id) as posts,
                COUNT(c.id) as deployed_comments,
                SUM(CASE WHEN c.mentions_brand = 1 THEN 1 ELSE 0 END) as branded_comments,
                SUM(CASE WHEN c.mentions_brand = 0 THEN 1 ELSE 0 END) as general_comments,
                SUM(CASE WHEN c.status = 'paid' THEN 1 ELSE 0 END) as paid_comments,
                SUM(CASE WHEN c.status = 'deployed' THEN 1 ELSE 0 END) as unpaid_comments
            FROM comments c
            JOIN posts p ON c.post_id = p.id
            JOIN subreddits s ON p.subreddit_id = s.id
            JOIN brands b ON c.brand_id = b.id
            WHERE {bq_where}
            GROUP BY b.id, b.name
            ORDER BY deployed_comments DESC
        """, bq_params).fetchall()]

        # Per-subreddit paid breakdown
        sub_stats = [dict(r) for r in self.conn.execute(f"""
            SELECT
                s.id as subreddit_id,
                s.name as subreddit_name,
                COUNT(c.id) as deployed_comments,
                SUM(CASE WHEN c.status = 'paid' THEN 1 ELSE 0 END) as paid_comments,
                SUM(CASE WHEN c.status = 'deployed' THEN 1 ELSE 0 END) as unpaid_comments
            FROM comments c
            JOIN posts p ON c.post_id = p.id
            JOIN subreddits s ON p.subreddit_id = s.id
            WHERE {bq_where}
            GROUP BY s.id, s.name
            ORDER BY deployed_comments DESC
        """, bq_params).fetchall()]

        return {"totals": totals, "accounts": accounts, "brand_stats": brand_stats, "sub_stats": sub_stats}

    def get_persona_distribution(self, subreddit_id=None, brand_name=None):
        query = "SELECT c.persona_id, COUNT(*) as cnt FROM comments c"
        params = []
        joins = []
        wheres = []

        if subreddit_id:
            joins.append("JOIN posts p ON c.post_id = p.id")
            wheres.append("p.subreddit_id = ?")
            params.append(subreddit_id)
        if brand_name:
            joins.append("JOIN brands b ON c.brand_id = b.id")
            wheres.append("LOWER(b.name) = LOWER(?)")
            params.append(brand_name)

        if joins:
            query += " " + " ".join(joins)
        if wheres:
            query += " WHERE " + " AND ".join(wheres)
        query += " GROUP BY c.persona_id ORDER BY cnt DESC"

        rows = self.conn.execute(query, params).fetchall()
        return {r["persona_id"]: r["cnt"] for r in rows}

    def get_brand_mention_ratio(self, subreddit_id=None, brand_name=None):
        query = """SELECT
            COUNT(*) as total,
            SUM(CASE WHEN c.mentions_brand = 1 THEN 1 ELSE 0 END) as with_brand
        FROM comments c"""
        params = []
        joins = []
        wheres = []

        if subreddit_id:
            joins.append("JOIN posts p ON c.post_id = p.id")
            wheres.append("p.subreddit_id = ?")
            params.append(subreddit_id)
        if brand_name:
            joins.append("JOIN brands b ON c.brand_id = b.id")
            wheres.append("LOWER(b.name) = LOWER(?)")
            params.append(brand_name)

        if joins:
            query += " " + " ".join(joins)
        if wheres:
            query += " WHERE " + " AND ".join(wheres)

        row = self.conn.execute(query, params).fetchone()
        total = row["total"] or 0
        with_brand = row["with_brand"] or 0
        return {"total": total, "with_brand": with_brand, "ratio": with_brand / total if total > 0 else 0}

    def _run_migrations(self):
        """Run schema migrations for columns added after initial release."""
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(comments)").fetchall()]
        migrations = {
            "is_ours": "ALTER TABLE comments ADD COLUMN is_ours INTEGER DEFAULT 1",
            "matched_keywords": "ALTER TABLE comments ADD COLUMN matched_keywords TEXT",
            "reddit_comment_url": "ALTER TABLE comments ADD COLUMN reddit_comment_url TEXT",
            "deployed_at": "ALTER TABLE comments ADD COLUMN deployed_at TEXT",
            "deleted_at": "ALTER TABLE comments ADD COLUMN deleted_at TEXT",
            "comment_type": "ALTER TABLE comments ADD COLUMN comment_type TEXT DEFAULT ''",
            "paid_at": "ALTER TABLE comments ADD COLUMN paid_at TEXT",
            "assigned_at": "ALTER TABLE comments ADD COLUMN assigned_at TEXT",
            "informed_at": "ALTER TABLE comments ADD COLUMN informed_at TEXT",
            "last_live_check": "ALTER TABLE comments ADD COLUMN last_live_check TEXT",
            "prev_status": "ALTER TABLE comments ADD COLUMN prev_status TEXT",
            # Brand-focus pairing tracking. focus_phrase is the phrase
            # assigned to this slot (NULL if no focus phrase was assigned).
            # focus_hit is 1 when the body contains both the brand and the
            # phrase within ~120 chars of each other (i.e. the AI-retriever
            # association formed); 0 when assigned-but-missed; NULL when
            # no phrase was assigned. See generators/comment_gen.py
            # _focus_pair_in_body for the matcher.
            "focus_phrase": "ALTER TABLE comments ADD COLUMN focus_phrase TEXT",
            "focus_hit": "ALTER TABLE comments ADD COLUMN focus_hit INTEGER",
            # Real Reddit-posted timestamp (Reddit's `created_utc`,
            # stored as "YYYY-MM-DD HH:MM:SS" UTC). NULL until the
            # bot has had a chance to fetch it. Distinct from
            # `deployed_at` (when the user clicked Deploy in this
            # bot) so we can show the actual publish time on Reddit
            # in the CSV export and any analytics.
            "posted_at": "ALTER TABLE comments ADD COLUMN posted_at TEXT",
            # Client reporting module — comments pushed to a monthly
            # client report. `report_month` is "YYYY-MM"; when set, the
            # row should be visible in the corresponding client's
            # monthly dashboard. `report_added_at` is the timestamp
            # the admin clicked "Send to Report". Both are NULL until
            # the row enters the report state. status='report' AND
            # report_month IS NOT NULL are written together.
            "report_month": "ALTER TABLE comments ADD COLUMN report_month TEXT",
            "report_added_at": "ALTER TABLE comments ADD COLUMN report_added_at TEXT",
            # Direct client assignment — set when admin pushes a row
            # to report state. Replaces the previous "deduce client
            # from brand_id" approach which silently broke when
            # comments had NULL brand_id or were stored on a brand
            # other than the one the admin meant for that client.
            # NULL = legacy rows (pre-client-picker); dashboard then
            # falls back to brand resolution for those.
            "report_client_id": "ALTER TABLE comments ADD COLUMN report_client_id INTEGER REFERENCES clients(id)",
            # Reddit engagement snapshot. Updated by the
            # /api/comments/live-stats task — and reused by the
            # client portal dashboard / CSV export. NULL until the
            # first successful fetch; `last_stats_at` lets the UI
            # show "as of …".
            "upvotes": "ALTER TABLE comments ADD COLUMN upvotes INTEGER",
            "num_replies": "ALTER TABLE comments ADD COLUMN num_replies INTEGER",
            "last_stats_at": "ALTER TABLE comments ADD COLUMN last_stats_at TEXT",
        }
        for col, sql in migrations.items():
            if col not in cols:
                self.conn.execute(sql)
                self.conn.commit()

        # Subreddit owner migration
        sub_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(subreddits)").fetchall()]
        if "owner_account" not in sub_cols:
            self.conn.execute("ALTER TABLE subreddits ADD COLUMN owner_account TEXT DEFAULT ''")
            self.conn.commit()
        # Live Subreddits module: marks rows that were auto-provisioned for a
        # brand-driven generation flow (so the regular Subreddits page can
        # distinguish them from manually-managed subreddits).
        if "is_live" not in sub_cols:
            self.conn.execute("ALTER TABLE subreddits ADD COLUMN is_live INTEGER DEFAULT 0")
            self.conn.commit()

        # Post owner migration
        post_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(posts)").fetchall()]
        if "owner_account" not in post_cols:
            self.conn.execute("ALTER TABLE posts ADD COLUMN owner_account TEXT DEFAULT ''")
            self.conn.commit()

        # Tasks table (for durable background task tracking)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                result TEXT,
                error TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

        # Search posts table (Live Search feature)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS search_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reddit_url TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                subreddit TEXT NOT NULL,
                score INTEGER DEFAULT 0,
                num_comments INTEGER DEFAULT 0,
                author TEXT DEFAULT '',
                post_date TEXT,
                body_preview TEXT DEFAULT '',
                brand_id INTEGER REFERENCES brands(id),
                status TEXT DEFAULT 'saved',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

        # Migration: make brands.subreddit_id nullable (allow standalone brands)
        brand_cols_info = self.conn.execute("PRAGMA table_info(brands)").fetchall()
        for col_info in brand_cols_info:
            if col_info[1] == 'subreddit_id' and col_info[3] == 1:  # notnull == 1
                self.conn.execute("PRAGMA foreign_keys = OFF")
                self.conn.executescript("""
                    CREATE TABLE IF NOT EXISTS brands_new (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        subreddit_id    INTEGER REFERENCES subreddits(id),
                        name            TEXT NOT NULL,
                        domain_url      TEXT,
                        context         TEXT NOT NULL,
                        keywords        TEXT,
                        added_at        TEXT DEFAULT (datetime('now'))
                    );
                    INSERT INTO brands_new SELECT * FROM brands;
                    DROP TABLE brands;
                    ALTER TABLE brands_new RENAME TO brands;
                """)
                self.conn.execute("PRAGMA foreign_keys = ON")
                break

        # Search comments table (Live Search feature)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS search_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_post_id INTEGER NOT NULL REFERENCES search_posts(id),
                brand_id INTEGER REFERENCES brands(id),
                account_id TEXT,
                body TEXT NOT NULL,
                persona_id TEXT,
                is_reply INTEGER DEFAULT 0,
                reply_to_url TEXT,
                mentions_brand INTEGER DEFAULT 0,
                relevance_score REAL,
                status TEXT DEFAULT 'draft',
                reddit_comment_url TEXT,
                deployed_at TEXT,
                deleted_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

        # search_comments migrations
        sc_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(search_comments)").fetchall()]
        for col, sql in {
            "paid_at": "ALTER TABLE search_comments ADD COLUMN paid_at TEXT",
            "assigned_at": "ALTER TABLE search_comments ADD COLUMN assigned_at TEXT",
            "informed_at": "ALTER TABLE search_comments ADD COLUMN informed_at TEXT",
            "last_live_check": "ALTER TABLE search_comments ADD COLUMN last_live_check TEXT",
            "prev_status": "ALTER TABLE search_comments ADD COLUMN prev_status TEXT",
            # HQ thread support: 1 parent (comment_type='hq', parent_comment_id NULL)
            # + N replies (comment_type='hq', parent_comment_id = parent's row id).
            "comment_type": "ALTER TABLE search_comments ADD COLUMN comment_type TEXT",
            "parent_comment_id": "ALTER TABLE search_comments ADD COLUMN parent_comment_id INTEGER REFERENCES search_comments(id)",
            # Real Reddit-posted timestamp — see the `comments` table
            # migration for the rationale. Same shape, separate table.
            "posted_at": "ALTER TABLE search_comments ADD COLUMN posted_at TEXT",
            # Client reporting module — same shape as the `comments`
            # migration. See the comments-table block for rationale.
            "report_month": "ALTER TABLE search_comments ADD COLUMN report_month TEXT",
            "report_added_at": "ALTER TABLE search_comments ADD COLUMN report_added_at TEXT",
            "report_client_id": "ALTER TABLE search_comments ADD COLUMN report_client_id INTEGER REFERENCES clients(id)",
            # See `comments` table for rationale. Same shape on
            # the search side so the portal can pull stats
            # uniformly across both pipelines.
            "upvotes": "ALTER TABLE search_comments ADD COLUMN upvotes INTEGER",
            "num_replies": "ALTER TABLE search_comments ADD COLUMN num_replies INTEGER",
            "last_stats_at": "ALTER TABLE search_comments ADD COLUMN last_stats_at TEXT",
        }.items():
            if col not in sc_cols:
                self.conn.execute(sql)
                self.conn.commit()

        # --- Client reporting module: new tables ---------------------
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS clients (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                password_hash   TEXT NOT NULL,
                monthly_target  INTEGER,
                notes           TEXT,
                created_at      TEXT DEFAULT (datetime('now')),
                last_login_at   TEXT
            );
            CREATE TABLE IF NOT EXISTS client_emails (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                email       TEXT NOT NULL,
                is_primary  INTEGER DEFAULT 0,
                UNIQUE(email)
            );
            CREATE TABLE IF NOT EXISTS client_brands (
                client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                brand_id    INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
                PRIMARY KEY (client_id, brand_id)
            );
            CREATE TABLE IF NOT EXISTS report_audit (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                comment_id    INTEGER,
                source        TEXT,
                action        TEXT,
                report_month  TEXT,
                prev_month    TEXT,
                actor_email   TEXT,
                created_at    TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_client_brands_brand ON client_brands(brand_id);
            CREATE INDEX IF NOT EXISTS idx_client_emails_client ON client_emails(client_id);
            CREATE INDEX IF NOT EXISTS idx_comments_report_month ON comments(report_month);
            CREATE INDEX IF NOT EXISTS idx_search_comments_report_month ON search_comments(report_month);

            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id       INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                token_hash      TEXT NOT NULL UNIQUE,
                expires_at      TEXT NOT NULL,                  -- ISO datetime UTC
                consumed_at     TEXT,
                requested_email TEXT,                            -- the email the user typed
                requested_ip    TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_pwd_reset_client ON password_reset_tokens(client_id);
            CREATE INDEX IF NOT EXISTS idx_pwd_reset_expires ON password_reset_tokens(expires_at);
        """)
        self.conn.commit()

        # paid_at migration for posts
        post_cols2 = [r[1] for r in self.conn.execute("PRAGMA table_info(posts)").fetchall()]
        if "paid_at" not in post_cols2:
            self.conn.execute("ALTER TABLE posts ADD COLUMN paid_at TEXT")
            self.conn.commit()
        if "deployed_at" not in post_cols2:
            self.conn.execute("ALTER TABLE posts ADD COLUMN deployed_at TEXT")
            self.conn.commit()
        # Live Subreddits separation: denormalize subreddits.is_live onto posts so
        # list/aggregation queries can filter without joining the subreddits table
        # every time. Backfill from the parent subreddit row once.
        if "is_live" not in post_cols2:
            self.conn.execute("ALTER TABLE posts ADD COLUMN is_live INTEGER DEFAULT 0")
            self.conn.commit()
            self.conn.execute(
                "UPDATE posts SET is_live = 1 "
                "WHERE subreddit_id IN (SELECT id FROM subreddits WHERE is_live = 1)"
            )
            self.conn.commit()
        # prev_status is used by the mark-removed / undo-removed flow
        # so the admin can revert a misclick. Comments already have
        # this; posts get it now.
        if "prev_status" not in post_cols2:
            self.conn.execute("ALTER TABLE posts ADD COLUMN prev_status TEXT")
            self.conn.commit()
        # Post-level monthly report stamp. The post lifecycle now has
        # a 'report' status (set by `move_post_to_report`); these
        # columns record which month the post was pushed into and
        # when. Mirrors the comments table fields. Dashboard query
        # still reads from `comments` for visibility — these columns
        # are for the admin Posts tab (badge, undo, lifecycle).
        if "report_month" not in post_cols2:
            self.conn.execute("ALTER TABLE posts ADD COLUMN report_month TEXT")
            self.conn.commit()
        if "report_added_at" not in post_cols2:
            self.conn.execute("ALTER TABLE posts ADD COLUMN report_added_at TEXT")
            self.conn.commit()
        # Real Reddit-side publish timestamp. `deployed_at` records
        # when the admin clicked Mark Published in our bot — that's
        # internal admin action, not the Reddit publish event. The
        # client dashboard's "Published Date" column should reflect
        # when the post actually went live on Reddit (Reddit's
        # `data.created_utc` from the post's `.json` payload).
        if "posted_at" not in post_cols2:
            self.conn.execute("ALTER TABLE posts ADD COLUMN posted_at TEXT")
            self.conn.commit()

        # One-shot backfill: ancient rows had `paid_at` set but
        # status still on the prior lifecycle step. We flip those
        # to 'paid' EXACTLY ONCE, gated on a meta flag so subsequent
        # startups don't undo deliberate post-paid status changes.
        #
        # CRITICAL: this used to run on every startup and the
        # WHERE clause was `status != 'paid'`. That clobbered the
        # report lifecycle — reported comments retain paid_at
        # (because they were paid before being reported), so the
        # migration kept reverting them to 'paid' on every restart
        # AND every background task (since background tasks call
        # initialize()→migrations). Two safeguards now:
        #  1. Meta-gated so it only ever runs once.
        #  2. NOT IN exclusion against every post-paid lifecycle
        #     status as defense-in-depth.
        # Ensure the meta table exists — it's created later in this
        # function but we need it here for the gating check below.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        if not self.meta_get("paid_status_backfill_v1"):
            POST_PAID = ('paid', 'report', 'removed', 'deleted')
            ph = ",".join("?" * len(POST_PAID))
            self.conn.execute(
                f"UPDATE comments SET status = 'paid' "
                f"WHERE paid_at IS NOT NULL AND status NOT IN ({ph})",
                POST_PAID,
            )
            self.conn.execute(
                f"UPDATE search_comments SET status = 'paid' "
                f"WHERE paid_at IS NOT NULL AND status NOT IN ({ph})",
                POST_PAID,
            )
            self.conn.execute(
                f"UPDATE posts SET status = 'paid' "
                f"WHERE paid_at IS NOT NULL AND status NOT IN ({ph})",
                POST_PAID,
            )
            self.conn.commit()
            self.meta_set("paid_status_backfill_v1", "1")

        # One-shot repair: rows that the broken paid-status migration
        # above clobbered. A reported row leaves `paid_at` set
        # (preserving when it was paid) AND stamps `report_month` /
        # `report_added_at`. The prior migration kept reverting them
        # to status='paid' every startup. Restore those: if a row
        # is in status='paid' but ALSO carries report_month, it was
        # almost certainly a victim — flip it back to 'report'.
        if not self.meta_get("report_status_repair_v1"):
            self.conn.execute(
                """UPDATE comments
                      SET status = 'report'
                    WHERE status = 'paid'
                      AND report_month IS NOT NULL
                      AND report_added_at IS NOT NULL"""
            )
            self.conn.execute(
                """UPDATE search_comments
                      SET status = 'report'
                    WHERE status = 'paid'
                      AND report_month IS NOT NULL
                      AND report_added_at IS NOT NULL"""
            )
            self.conn.commit()
            self.meta_set("report_status_repair_v1", "1")

        # One-shot cleanup: the report model is now "1 post = 1 HQ
        # root comment reported". Earlier flows could flip every
        # deployed/paid comment under a reported post (including HQ
        # replies + organics) to status='report' — those are extras
        # under the new model. For every comment in status='report'
        # whose parent post is also in status='report', revert it
        # back to its prev_status UNLESS it's the deployed HQ root.
        # Idempotent via meta gate.
        if not self.meta_get("report_extras_cleanup_v1"):
            extras = self.conn.execute(
                """SELECT c.id, c.prev_status, c.paid_at
                     FROM comments c
                     JOIN posts p ON c.post_id = p.id
                    WHERE c.status = 'report'
                      AND p.status = 'report'
                      AND NOT (
                          c.comment_type = 'hq'
                          AND c.parent_comment_id IS NULL
                          AND TRIM(COALESCE(c.reddit_comment_url, '')) != ''
                      )"""
            ).fetchall()
            reverted = 0
            for r in extras:
                # Restore the comment to its previous lifecycle step.
                # prev_status was preserved when move_comment_to_report
                # ran; fall back to 'paid' if it's NULL AND paid_at
                # is set, else 'deployed'.
                restore = r["prev_status"]
                if not restore:
                    restore = "paid" if r["paid_at"] else "deployed"
                self.conn.execute(
                    """UPDATE comments
                          SET status = ?,
                              prev_status = NULL,
                              report_month = NULL,
                              report_added_at = NULL
                        WHERE id = ?""",
                    (restore, r["id"])
                )
                reverted += 1
            if reverted:
                self.conn.commit()
                print(f"[migrations] report_extras_cleanup_v1: reverted {reverted} non-HQ-root reported comments", flush=True)
            self.meta_set("report_extras_cleanup_v1", "1")

        # Subreddit scrutiny cache (comment removal rate + gate penalty)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS subreddit_scrutiny (
                name TEXT PRIMARY KEY,
                subscribers INTEGER,
                comment_removal_rate REAL,
                post_removal_rate REAL,
                gate_penalty REAL,
                scrutiny_score REAL,
                subreddit_type TEXT,
                computed_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

        # Backfill post_brands from posts.brand_id for existing data
        if self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='post_brands'").fetchone():
            existing = self.conn.execute("SELECT COUNT(*) as c FROM post_brands").fetchone()["c"]
            if existing == 0:
                self.conn.execute("""
                    INSERT OR IGNORE INTO post_brands (post_id, brand_id)
                    SELECT id, brand_id FROM posts WHERE brand_id IS NOT NULL
                """)
                self.conn.commit()

        # ----- accounts: karma-refresh diagnostics + lifetime rotation counter -----
        acct_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(accounts)").fetchall()]
        if "last_refresh_attempt" not in acct_cols:
            self.conn.execute("ALTER TABLE accounts ADD COLUMN last_refresh_attempt TEXT")
            self.conn.commit()
        if "last_refresh_error" not in acct_cols:
            self.conn.execute("ALTER TABLE accounts ADD COLUMN last_refresh_error TEXT")
            self.conn.commit()
        if "lifetime_assignments" not in acct_cols:
            self.conn.execute(
                "ALTER TABLE accounts ADD COLUMN lifetime_assignments INTEGER NOT NULL DEFAULT 0"
            )
            # One-time backfill from existing state (runs exactly once because the
            # ALTER TABLE guard above only fires on first migration).
            self.conn.execute(
                """UPDATE accounts SET lifetime_assignments = (
                       COALESCE((SELECT COUNT(*) FROM comments
                                  WHERE account_id = accounts.username
                                    AND status != 'draft'), 0)
                     + COALESCE((SELECT COUNT(*) FROM search_comments
                                  WHERE account_id = accounts.username
                                    AND status NOT IN ('draft','deleted')), 0)
                     + COALESCE((SELECT COUNT(*) FROM posts
                                  WHERE owner_account = accounts.username), 0)
                   )"""
            )
            self.conn.commit()
        if "excluded" not in acct_cols:
            self.conn.execute("ALTER TABLE accounts ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()
        if "assign_seq" not in acct_cols:
            self.conn.execute("ALTER TABLE accounts ADD COLUMN assign_seq INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()

        # Re-sync lifetime_assignments from actual DB state (fixes drift caused
        # by delete_subreddit/delete_account paths that previously skipped
        # _decrement_lifetime calls).
        if not self.meta_get("lifetime_backfill_v2"):
            self.conn.execute(
                """UPDATE accounts SET lifetime_assignments = (
                       COALESCE((SELECT COUNT(*) FROM comments
                                  WHERE account_id = accounts.username
                                    AND status NOT IN ('draft', 'complete')), 0)
                     + COALESCE((SELECT COUNT(*) FROM search_comments
                                  WHERE account_id = accounts.username
                                    AND status NOT IN ('draft','deleted')), 0)
                     + COALESCE((SELECT COUNT(*) FROM posts
                                  WHERE owner_account = accounts.username), 0)
                   )"""
            )
            self.conn.commit()
            self.meta_set("lifetime_backfill_v2", "1")

        # ----- brands: GEO enrichment columns -----
        brand_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(brands)").fetchall()]
        brand_enrichment_cols = {
            "category":          "ALTER TABLE brands ADD COLUMN category TEXT",
            "audience":          "ALTER TABLE brands ADD COLUMN audience TEXT",
            "use_cases":         "ALTER TABLE brands ADD COLUMN use_cases TEXT",
            "pain_points":       "ALTER TABLE brands ADD COLUMN pain_points TEXT",
            "features":          "ALTER TABLE brands ADD COLUMN features TEXT",
            "competitors":       "ALTER TABLE brands ADD COLUMN competitors TEXT",
            "enriched_at":       "ALTER TABLE brands ADD COLUMN enriched_at TEXT",
            "search_subreddits": "ALTER TABLE brands ADD COLUMN search_subreddits TEXT",
            # User-supplied editorial direction for the comment voice —
            # phrases the generator should weave in where they naturally
            # fit. JSON-serialised array of strings. NULL/empty = no
            # focus, generator behaviour unchanged.
            "focus":             "ALTER TABLE brands ADD COLUMN focus TEXT",
            # Anchor-scoped knowledge learned on cluster creation: what THIS
            # brand offers for a given seed/anchor topic. JSON keyed by
            # seed_norm: {seed_norm: {anchor, summary, covers, key_points, added_at}}.
            # Accumulates over time; fed into generation. Separate from the
            # user's manual `context`, which is never overwritten.
            "learned_context":   "ALTER TABLE brands ADD COLUMN learned_context TEXT",
            # Auto-generated buyer personas (ICPs) for persona-aware fan-out. JSON
            # list of {label, profile, trigger, goal, constraints, vocab, fit}.
            # fit ∈ {yes,maybe,no} = is the brand a credible answer for this persona.
            "personas":          "ALTER TABLE brands ADD COLUMN personas TEXT",
            # Map of competitor name → official homepage domain (JSON), captured at
            # Auto-analyze. Used by the blog generator to auto-fetch + cite competitor
            # sites. `competitors` (names) stays as-is for existing consumers.
            "competitor_domains": "ALTER TABLE brands ADD COLUMN competitor_domains TEXT",
            # EEAT byline (brand-supplied, never fabricated) — rendered on blogs when set.
            "author_name":       "ALTER TABLE brands ADD COLUMN author_name TEXT",
            "author_title":      "ALTER TABLE brands ADD COLUMN author_title TEXT",
            "reviewer_name":     "ALTER TABLE brands ADD COLUMN reviewer_name TEXT",
            "reviewer_title":    "ALTER TABLE brands ADD COLUMN reviewer_title TEXT",
            "disclosure":        "ALTER TABLE brands ADD COLUMN disclosure TEXT",
        }
        for col, sql in brand_enrichment_cols.items():
            if col not in brand_cols:
                self.conn.execute(sql)
                self.conn.commit()

        # ----- blogs: evidence inputs (source URLs + research notes) -----
        blog_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(blogs)").fetchall()]
        for col in ("source_urls", "research_notes"):
            if col not in blog_cols:
                self.conn.execute(f"ALTER TABLE blogs ADD COLUMN {col} TEXT")
                self.conn.commit()
        if "use_web_search" not in blog_cols:
            self.conn.execute("ALTER TABLE blogs ADD COLUMN use_web_search INTEGER DEFAULT 0")
            self.conn.commit()

        # ----- posts: intent column for GEO-style 1:1:1 batches -----
        post_cols3 = [r[1] for r in self.conn.execute("PRAGMA table_info(posts)").fetchall()]
        if "intent" not in post_cols3:
            self.conn.execute("ALTER TABLE posts ADD COLUMN intent TEXT")
            self.conn.commit()
        # ----- posts: concept_checklist (JSON) for AI-Search semantic-coverage
        #       mode — the per-post phrasing checklist the body (and later the HQ
        #       anchor) should cover so the thread is retrievable for the whole
        #       query cluster. NULL for standard-mode posts. -----
        if "concept_checklist" not in post_cols3:
            self.conn.execute("ALTER TABLE posts ADD COLUMN concept_checklist TEXT")
            self.conn.commit()
        # ----- posts: ai_search_meta (JSON) — the root reference for an
        #       AI-Search post: {seed, anchor, target_query}. Surfaced in the
        #       post-detail modal. NULL for standard-mode posts. -----
        if "ai_search_meta" not in post_cols3:
            self.conn.execute("ALTER TABLE posts ADD COLUMN ai_search_meta TEXT")
            self.conn.commit()
        # ----- posts: post_number — a stable per-brand sequential number
        #       (1,2,3...) over LIVE posts, in creation order. Replaces the old
        #       "suggested_post_day" recommendation in the Live Subs UI. Same
        #       post → same number across the Generate / Posts / Comments tabs.
        #       NULL for non-live posts. -----
        if "post_number" not in post_cols3:
            self.conn.execute("ALTER TABLE posts ADD COLUMN post_number INTEGER")
            self.conn.commit()
            # One-time backfill: number existing live posts per brand by age.
            rows = self.conn.execute(
                "SELECT id, brand_id FROM posts WHERE COALESCE(is_live,0)=1 "
                "ORDER BY brand_id, created_at, id"
            ).fetchall()
            counters = {}
            for r in rows:
                bid = r["brand_id"]
                counters[bid] = counters.get(bid, 0) + 1
                self.conn.execute(
                    "UPDATE posts SET post_number=? WHERE id=?",
                    (counters[bid], r["id"]))
            self.conn.commit()

        # ----- ai_search_clusters: persisted fan-out cluster per (brand, seed)
        #       so AI-Search "generate more" fills gaps against a STABLE cluster
        #       (exact X/N coverage) instead of re-deriving each run. -----
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_search_clusters (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id      INTEGER,
                seed_norm     TEXT NOT NULL,
                seed          TEXT,
                anchor        TEXT,
                rewrites_json TEXT,
                checklist_json TEXT,
                created_at    TEXT DEFAULT (datetime('now')),
                UNIQUE(brand_id, seed_norm)
            )
        """)
        self.conn.commit()
        # seed (original, un-normalized text) for display in the Clusters view;
        # backfilled flag = reconstructed from existing posts (rewrites = covered
        # only, original fan-out gaps unknown).
        clu_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(ai_search_clusters)").fetchall()]
        if "seed" not in clu_cols:
            self.conn.execute("ALTER TABLE ai_search_clusters ADD COLUMN seed TEXT")
            self.conn.commit()
        if "backfilled" not in clu_cols:
            self.conn.execute("ALTER TABLE ai_search_clusters ADD COLUMN backfilled INTEGER DEFAULT 0")
            self.conn.commit()

        # ----- app_meta: small key/value store for one-time startup flags -----
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS app_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS check_live_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                comment_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                reddit_url TEXT,
                action TEXT NOT NULL,
                prev_status TEXT NOT NULL,
                new_status TEXT NOT NULL,
                account_id TEXT,
                subreddit TEXT,
                brand_name TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Standalone "Check Live" runs — admin pastes a sheet URL,
        # names the run, and we record per-URL liveness without
        # touching any comment rows in the main DB. Each run has its
        # own header (status/counts) and a per-URL detail table.
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS check_live_runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL,
                sheet_url     TEXT,
                status        TEXT NOT NULL DEFAULT 'running', -- running | complete | error
                started_at    TEXT DEFAULT (datetime('now')),
                completed_at  TEXT,
                total         INTEGER DEFAULT 0,
                live_count    INTEGER DEFAULT 0,
                removed_count INTEGER DEFAULT 0,
                missing_count INTEGER DEFAULT 0,
                error_count   INTEGER DEFAULT 0,
                error_detail  TEXT
            );
            CREATE TABLE IF NOT EXISTS check_live_run_results (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id        INTEGER NOT NULL REFERENCES check_live_runs(id) ON DELETE CASCADE,
                url           TEXT NOT NULL,
                comment_id    TEXT,
                post_id       TEXT,
                liveness      TEXT,   -- live | removed | missing | error
                detail        TEXT,
                created_at    TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_check_live_run_results_run
                ON check_live_run_results(run_id);
        """)
        self.conn.commit()

        # ----- tasks: progress column for live updates -----
        task_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "progress" not in task_cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN progress TEXT")
            self.conn.commit()

        # Performance indexes
        perf_indexes = [
            "CREATE INDEX IF NOT EXISTS idx_brands_subreddit ON brands(subreddit_id)",
            "CREATE INDEX IF NOT EXISTS idx_comments_account ON comments(account_id)",
            "CREATE INDEX IF NOT EXISTS idx_comments_deployed_at ON comments(deployed_at)",
            "CREATE INDEX IF NOT EXISTS idx_comments_brand ON comments(brand_id)",
            "CREATE INDEX IF NOT EXISTS idx_comments_mentions ON comments(mentions_brand)",
            "CREATE INDEX IF NOT EXISTS idx_comments_type ON comments(comment_type)",
            "CREATE INDEX IF NOT EXISTS idx_comments_paid_at ON comments(paid_at)",
            "CREATE INDEX IF NOT EXISTS idx_comments_created_at ON comments(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_comments_status_deployed ON comments(status, deployed_at)",
            "CREATE INDEX IF NOT EXISTS idx_comments_status_paid ON comments(status, paid_at)",
            "CREATE INDEX IF NOT EXISTS idx_search_comments_account ON search_comments(account_id)",
            "CREATE INDEX IF NOT EXISTS idx_search_comments_status ON search_comments(status)",
            "CREATE INDEX IF NOT EXISTS idx_search_comments_deployed_at ON search_comments(deployed_at)",
            "CREATE INDEX IF NOT EXISTS idx_search_comments_paid_at ON search_comments(paid_at)",
            "CREATE INDEX IF NOT EXISTS idx_search_comments_status_deployed ON search_comments(status, deployed_at)",
            "CREATE INDEX IF NOT EXISTS idx_search_comments_status_paid ON search_comments(status, paid_at)",
            "CREATE INDEX IF NOT EXISTS idx_post_urls_post ON post_urls(post_id)",
            "CREATE INDEX IF NOT EXISTS idx_post_urls_added_at ON post_urls(added_at)",
            "CREATE INDEX IF NOT EXISTS idx_posts_paid_at ON posts(paid_at)",
            "CREATE INDEX IF NOT EXISTS idx_posts_owner_status ON posts(owner_account, status)",
            # Auto-assign context: composite indexes to avoid full-table scans
            "CREATE INDEX IF NOT EXISTS idx_comments_status_account ON comments(status, account_id)",
            "CREATE INDEX IF NOT EXISTS idx_search_comments_status_account ON search_comments(status, account_id)",
            "CREATE INDEX IF NOT EXISTS idx_comments_mentions_account_brand ON comments(mentions_brand, account_id, brand_id)",
            "CREATE INDEX IF NOT EXISTS idx_search_comments_mentions_account_brand ON search_comments(mentions_brand, account_id, brand_id)",
            "CREATE INDEX IF NOT EXISTS idx_comments_post_status ON comments(post_id, status, account_id)",
            "CREATE INDEX IF NOT EXISTS idx_search_comments_post_status ON search_comments(search_post_id, status, account_id)",
            "CREATE INDEX IF NOT EXISTS idx_posts_subreddit ON posts(subreddit_id)",
            "CREATE INDEX IF NOT EXISTS idx_search_posts_subreddit ON search_posts(subreddit)",
        ]
        for idx_sql in perf_indexes:
            self.conn.execute(idx_sql)
        self.conn.commit()

        # One-shot: recover brand_id on already-reported rows that
        # were written via the old report_client_id workaround.
        # See _backfill_report_brand_ids for the rules.
        try:
            self._backfill_report_brand_ids()
        except Exception as e:
            # Migrations must never block startup. Log and move on.
            print(f"[migrations] backfill_report_brand_ids failed: {e}", flush=True)

    def _backfill_report_brand_ids(self):
        """Legacy rows reported during the brief `report_client_id`
        experiment may carry a client link but no `brand_id`, so the
        new brand-only dashboard query won't find them.

        For every report/removed row with NULL brand_id AND a
        report_client_id pointing to a client with exactly ONE brand
        in `client_brands`, set the row's brand_id to that brand.
        Multi-brand-client rows are left untouched — the admin can
        Undo + Re-report them with an explicit brand pick.

        Idempotent: a second run is a no-op because the WHERE clause
        filters on brand_id IS NULL.
        """
        # Skip if the legacy column was never added (e.g. fresh DB
        # after we eventually drop it).
        c_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(comments)").fetchall()]
        sc_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(search_comments)").fetchall()]
        if "report_client_id" not in c_cols and "report_client_id" not in sc_cols:
            return
        # Single-brand-client lookup. We rely on the existence of the
        # client_brands table (created earlier in _run_migrations).
        single_brand_clients = {
            int(r["client_id"]): int(r["brand_id"])
            for r in self.conn.execute(
                """SELECT client_id, MIN(brand_id) AS brand_id
                     FROM client_brands
                    GROUP BY client_id
                   HAVING COUNT(*) = 1"""
            ).fetchall()
        }
        if not single_brand_clients:
            return
        if "report_client_id" in c_cols:
            rows = self.conn.execute(
                """SELECT id, report_client_id FROM comments
                    WHERE brand_id IS NULL
                      AND report_client_id IS NOT NULL
                      AND status IN ('report', 'removed')"""
            ).fetchall()
            for r in rows:
                bid = single_brand_clients.get(int(r["report_client_id"]))
                if bid:
                    self.conn.execute(
                        "UPDATE comments SET brand_id = ? WHERE id = ? AND brand_id IS NULL",
                        (bid, r["id"])
                    )
        if "report_client_id" in sc_cols:
            rows = self.conn.execute(
                """SELECT id, report_client_id FROM search_comments
                    WHERE brand_id IS NULL
                      AND report_client_id IS NOT NULL
                      AND status IN ('report', 'removed')"""
            ).fetchall()
            for r in rows:
                bid = single_brand_clients.get(int(r["report_client_id"]))
                if bid:
                    self.conn.execute(
                        "UPDATE search_comments SET brand_id = ? WHERE id = ? AND brand_id IS NULL",
                        (bid, r["id"])
                    )
        self.conn.commit()

    # --- Background Tasks ---

    def create_task(self, task_id, task_type):
        self.conn.execute(
            "INSERT INTO tasks (id, type, status) VALUES (?, ?, 'running')",
            (task_id, task_type)
        )
        self.conn.commit()

    def update_task(self, task_id, status, result=None, error=None):
        self.conn.execute(
            "UPDATE tasks SET status=?, result=?, error=? WHERE id=?",
            (status, json.dumps(result) if result is not None else None, error, task_id)
        )
        self.conn.commit()

    def update_task_progress(self, task_id, progress):
        self.conn.execute(
            "UPDATE tasks SET progress=? WHERE id=?",
            (json.dumps(progress), task_id)
        )
        self.conn.commit()

    def get_task(self, task_id):
        row = self.conn.execute(
            "SELECT id, type, status, result, error, progress FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "status": row["status"],
            "type": row["type"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": row["error"],
            "progress": json.loads(row["progress"]) if row["progress"] else None
        }

    def get_running_tasks(self):
        rows = self.conn.execute(
            "SELECT id, type, progress FROM tasks WHERE status='running'"
        ).fetchall()
        return [{"id": r["id"], "type": r["type"],
                 "progress": json.loads(r["progress"]) if r["progress"] else None}
                for r in rows]

    def cleanup_old_tasks(self, hours=24):
        self.conn.execute(
            "DELETE FROM tasks WHERE created_at < datetime('now', ?)",
            (f'-{hours} hours',)
        )
        self.conn.commit()

    # --- Check Live runs (admin-side standalone health checker) ---

    def create_check_live_run(self, name, sheet_url):
        cur = self.conn.execute(
            "INSERT INTO check_live_runs (name, sheet_url) VALUES (?, ?)",
            (name, sheet_url)
        )
        self.conn.commit()
        return cur.lastrowid

    def add_check_live_result(self, run_id, *, url, comment_id, post_id, liveness, detail=None):
        self.conn.execute(
            """INSERT INTO check_live_run_results
                 (run_id, url, comment_id, post_id, liveness, detail)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, url, comment_id, post_id, liveness, detail)
        )
        # Bump the parent run's counters in the same transaction so a
        # live progress polls see the running totals.
        col = {
            'live': 'live_count', 'removed': 'removed_count',
            'missing': 'missing_count',
        }.get(liveness, 'error_count')
        self.conn.execute(
            f"UPDATE check_live_runs SET total = total + 1, {col} = {col} + 1 WHERE id = ?",
            (run_id,)
        )
        self.conn.commit()

    def finish_check_live_run(self, run_id, status='complete', error_detail=None):
        self.conn.execute(
            """UPDATE check_live_runs
                  SET status = ?, completed_at = datetime('now'), error_detail = ?
                WHERE id = ?""",
            (status, error_detail, run_id)
        )
        self.conn.commit()

    def list_check_live_runs(self, limit=50):
        rows = self.conn.execute(
            """SELECT id, name, sheet_url, status, started_at, completed_at,
                      total, live_count, removed_count, missing_count, error_count
                 FROM check_live_runs
                ORDER BY id DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_check_live_run(self, run_id):
        head = self.conn.execute(
            "SELECT * FROM check_live_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not head:
            return None
        out = dict(head)
        rows = self.conn.execute(
            """SELECT url, comment_id, post_id, liveness, detail, created_at
                 FROM check_live_run_results
                WHERE run_id = ?
                ORDER BY id ASC""",
            (run_id,)
        ).fetchall()
        out["results"] = [dict(r) for r in rows]
        return out

    def delete_check_live_run(self, run_id):
        # Cascade is set in schema, so just delete the header.
        self.conn.execute("DELETE FROM check_live_runs WHERE id = ?", (run_id,))
        self.conn.commit()

    def mark_comment_ours(self, comment_id, is_ours):
        self.conn.execute("UPDATE comments SET is_ours = ? WHERE id = ?", (1 if is_ours else 0, comment_id))
        self.conn.commit()

    # --- Comment Lifecycle ---

    def assign_comment(self, comment_id, account_id):
        # Read prior account so we can correctly adjust the lifetime counter
        # if the comment was already assigned to someone else (reassignment).
        row = self.conn.execute(
            "SELECT account_id, status FROM comments WHERE id = ?", (comment_id,)
        ).fetchone()
        prior = row["account_id"] if row else None
        old_status = row["status"] if row else None
        self.conn.execute(
            "UPDATE comments SET account_id = ?, status = 'assigned', prev_status = ?, assigned_at = datetime('now'), "
            "paid_at = NULL, deleted_at = NULL, reddit_comment_url = NULL, deployed_at = NULL WHERE id = ?",
            (account_id, old_status, comment_id)
        )
        if prior and prior != account_id:
            self._decrement_lifetime(prior)
        if account_id and account_id != prior:
            self._increment_lifetime(account_id)
        self.conn.commit()

    def unassign_comment(self, comment_id):
        row = self.conn.execute(
            "SELECT account_id FROM comments WHERE id = ?", (comment_id,)
        ).fetchone()
        prior = row["account_id"] if row else None
        self.conn.execute(
            "UPDATE comments SET account_id = NULL, status = 'draft' WHERE id = ?",
            (comment_id,)
        )
        if prior:
            self._decrement_lifetime(prior)
        self.conn.commit()

    def reassign_comment(self, comment_id, new_account_id):
        """Change account_id on an already-assigned/informed comment without
        resetting status, assigned_at, informed_at, or prev_status."""
        row = self.conn.execute(
            "SELECT account_id FROM comments WHERE id = ?", (comment_id,)
        ).fetchone()
        prior = row["account_id"] if row else None
        if prior == new_account_id:
            return
        self.conn.execute(
            "UPDATE comments SET account_id = ? WHERE id = ?",
            (new_account_id, comment_id)
        )
        if prior:
            self._decrement_lifetime(prior)
        if new_account_id:
            self._increment_lifetime(new_account_id)
        self.conn.commit()

    def deploy_comment(self, comment_id, reddit_comment_url, deployed_at=None,
                       posted_at=None):
        """Mark a comment deployed.

        `deployed_at` is the bot's bookkeeping time (when the user
        clicked Deploy). `posted_at` is the actual Reddit publish
        timestamp (Reddit's `created_utc`) — passed in when the
        caller already has the comment JSON (bulk deploy, single-
        deploy async patch, backfill). It's never overwritten with
        NULL: if the caller doesn't have it yet, any previous value
        is preserved.
        """
        if not deployed_at:
            deployed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = self.conn.execute("SELECT status FROM comments WHERE id = ?", (comment_id,)).fetchone()
        old_status = row["status"] if row else None
        if posted_at:
            self.conn.execute(
                """UPDATE comments
                   SET reddit_comment_url = ?, deployed_at = ?, posted_at = ?,
                       status = 'deployed', prev_status = ?
                   WHERE id = ?""",
                (reddit_comment_url, deployed_at, posted_at, old_status, comment_id)
            )
        else:
            self.conn.execute(
                """UPDATE comments
                   SET reddit_comment_url = ?, deployed_at = ?,
                       status = 'deployed', prev_status = ?
                   WHERE id = ?""",
                (reddit_comment_url, deployed_at, old_status, comment_id)
            )
        self.conn.commit()

    def update_posted_at(self, comment_id, kind, posted_at):
        """Patch the real Reddit-posted timestamp on a comment row.

        `kind` is "comment" (→ `comments` table) or "search_comment"
        (→ `search_comments` table). NULL-safe: a NULL `posted_at`
        is a no-op (we don't overwrite an existing value with NULL).
        A non-NULL value REPLACES whatever is there.
        """
        if not posted_at or not comment_id:
            return
        if kind == "comment":
            self.conn.execute(
                "UPDATE comments SET posted_at = ? WHERE id = ?",
                (posted_at, comment_id)
            )
        elif kind == "search_comment":
            self.conn.execute(
                "UPDATE search_comments SET posted_at = ? WHERE id = ?",
                (posted_at, comment_id)
            )
        else:
            return
        self.conn.commit()

    def set_post_posted_at(self, post_id, posted_at):
        """Stamp `posts.posted_at` from Reddit's `created_utc`.

        Idempotent: only writes when the current value is NULL so
        backfills don't clobber values from earlier (more authoritative)
        runs. Silent on missing row / bad input.
        """
        if not post_id or not posted_at:
            return
        try:
            self.conn.execute(
                """UPDATE posts SET posted_at = ?
                    WHERE id = ? AND posted_at IS NULL""",
                (posted_at, post_id)
            )
            self.conn.commit()
        except Exception as e:
            print(f"[posts.posted_at] persist failed pid={post_id}: {e}", flush=True)

    def update_live_stats_by_id_url(self, comment_id, reddit_url, upvotes, num_replies):
        """Persist a live-stats fetch result. Uses both id AND URL to
        avoid cross-table collisions (same pattern as
        `update_posted_at_by_id_url`). Writes to whichever table the
        (id, url) pair matches; a NULL url means "skip" rather than
        falling back to id-only (too risky).

        Both `upvotes` and `num_replies` can be NULL — in that case
        we still bump `last_stats_at` so the UI can show that we
        tried. Callers should pass through whatever Reddit returned
        (including 0).
        """
        if not comment_id or not reddit_url:
            return
        try:
            self.conn.execute(
                """UPDATE comments
                      SET upvotes = ?, num_replies = ?,
                          last_stats_at = datetime('now')
                    WHERE id = ? AND reddit_comment_url = ?""",
                (upvotes, num_replies, comment_id, reddit_url)
            )
            self.conn.execute(
                """UPDATE search_comments
                      SET upvotes = ?, num_replies = ?,
                          last_stats_at = datetime('now')
                    WHERE id = ? AND reddit_comment_url = ?""",
                (upvotes, num_replies, comment_id, reddit_url)
            )
            self.conn.commit()
        except Exception as e:
            print(f"[live-stats] persist failed cid={comment_id}: {e}", flush=True)

    def update_posted_at_by_id_url(self, comment_id, reddit_url, posted_at):
        """Side-channel patch used by the live-stats / piggy-back path
        where we have a row id and the URL but don't know which table
        the id belongs to.

        Updates `comments` or `search_comments` only where BOTH the id
        AND the URL match — so a coincidental id collision across the
        two tables can't write to the wrong row. Only fills in
        `posted_at` when it's currently NULL (we don't overwrite an
        existing value here; that's reserved for the explicit deploy
        path).
        """
        if not posted_at or not comment_id or not reddit_url:
            return
        self.conn.execute(
            """UPDATE comments SET posted_at = ?
               WHERE id = ? AND reddit_comment_url = ? AND posted_at IS NULL""",
            (posted_at, comment_id, reddit_url)
        )
        self.conn.execute(
            """UPDATE search_comments SET posted_at = ?
               WHERE id = ? AND reddit_comment_url = ? AND posted_at IS NULL""",
            (posted_at, comment_id, reddit_url)
        )
        self.conn.commit()

    def list_deployed_missing_posted_at(self, brand_id=None, subreddit_id=None,
                                        limit=500):
        """Return rows that are deployed AND missing `posted_at` AND have
        a `reddit_comment_url`. Used by the backfill task.

        Returns a list of dicts with keys: id, kind ('comment' or
        'search_comment'), reddit_comment_url. Caps at `limit` rows per
        call so the backfill task can stream in pages.
        """
        rows = []
        q1 = """SELECT c.id, c.reddit_comment_url, b.id AS brand_id, p.subreddit_id
                FROM comments c
                JOIN posts p ON c.post_id = p.id
                LEFT JOIN brands b ON c.brand_id = b.id
                WHERE c.status = 'deployed'
                  AND c.posted_at IS NULL
                  AND c.reddit_comment_url IS NOT NULL
                  AND c.reddit_comment_url != ''"""
        p1 = []
        if brand_id:
            q1 += " AND c.brand_id = ?"
            p1.append(brand_id)
        if subreddit_id:
            q1 += " AND p.subreddit_id = ?"
            p1.append(subreddit_id)
        q1 += " LIMIT ?"
        p1.append(limit)
        for r in self.conn.execute(q1, p1).fetchall():
            rows.append({
                "id": r["id"], "kind": "comment",
                "reddit_comment_url": r["reddit_comment_url"],
            })
        q2 = """SELECT sc.id, sc.reddit_comment_url
                FROM search_comments sc
                WHERE sc.status = 'deployed'
                  AND sc.posted_at IS NULL
                  AND sc.reddit_comment_url IS NOT NULL
                  AND sc.reddit_comment_url != ''"""
        p2 = []
        if brand_id:
            q2 += " AND sc.brand_id = ?"
            p2.append(brand_id)
        q2 += " LIMIT ?"
        p2.append(limit)
        for r in self.conn.execute(q2, p2).fetchall():
            rows.append({
                "id": r["id"], "kind": "search_comment",
                "reddit_comment_url": r["reddit_comment_url"],
            })
        return rows

    # ------------------------------------------------------------------
    # Bulk-deploy URL → row matching helpers.
    # The Bulk Deploy feature walks a list of Reddit URLs from a sheet
    # and needs to map each URL back to its `comments` / `search_comments`
    # / `posts` / `search_posts` row. Tier-1 in the bulk-deploy matcher
    # is a direct `reddit_comment_url` lookup; Tier-2 falls back to body
    # fuzzy matching scoped to the same post.
    # ------------------------------------------------------------------

    def find_comment_by_url(self, reddit_url):
        """Return the legacy `comments` row whose reddit_comment_url is the
        given URL, or None. Used by bulk deploy's Tier-1 matcher.
        """
        row = self.conn.execute(
            "SELECT * FROM comments WHERE reddit_comment_url = ? LIMIT 1",
            (reddit_url,)
        ).fetchone()
        return dict(row) if row else None

    def find_search_comment_by_url(self, reddit_url):
        """Return the `search_comments` row whose reddit_comment_url is the
        given URL, or None.
        """
        row = self.conn.execute(
            "SELECT * FROM search_comments WHERE reddit_comment_url = ? LIMIT 1",
            (reddit_url,)
        ).fetchone()
        return dict(row) if row else None

    def find_comment_by_reddit_comment_id(self, comment_id):
        """Locate a legacy `comments` row whose stored URL contains the
        given Reddit comment ID anywhere in its path.

        Reddit comment IDs are globally unique (6-7 alphanumeric chars),
        so substring-matching on the stored `reddit_comment_url` is
        safe and catches stored URL variants that exact-match would
        miss (different slug, trailing slash, query string, etc.).
        """
        if not comment_id:
            return None
        like_a = f"%/{comment_id}"
        like_b = f"%/{comment_id}/%"
        like_c = f"%/{comment_id}?%"
        row = self.conn.execute(
            """SELECT * FROM comments
               WHERE reddit_comment_url LIKE ? OR reddit_comment_url LIKE ?
                  OR reddit_comment_url LIKE ?
               LIMIT 1""",
            (like_a, like_b, like_c)
        ).fetchone()
        return dict(row) if row else None

    def find_search_comment_by_reddit_comment_id(self, comment_id):
        """Same as `find_comment_by_reddit_comment_id` but on the
        `search_comments` table.
        """
        if not comment_id:
            return None
        like_a = f"%/{comment_id}"
        like_b = f"%/{comment_id}/%"
        like_c = f"%/{comment_id}?%"
        row = self.conn.execute(
            """SELECT * FROM search_comments
               WHERE reddit_comment_url LIKE ? OR reddit_comment_url LIKE ?
                  OR reddit_comment_url LIKE ?
               LIMIT 1""",
            (like_a, like_b, like_c)
        ).fetchone()
        return dict(row) if row else None

    def find_post_by_url(self, reddit_url):
        """Return (kind, post_dict) for a Reddit post URL.

        Looks in `post_urls` first (legacy posts) then `search_posts`.
        `kind` is "post" for legacy or "search_post" for Live Search.
        Returns (None, None) on no match.
        """
        row = self.conn.execute(
            """SELECT p.*, pu.reddit_url
               FROM post_urls pu JOIN posts p ON pu.post_id = p.id
               WHERE pu.reddit_url = ? LIMIT 1""",
            (reddit_url,)
        ).fetchone()
        if row:
            return "post", dict(row)
        row = self.conn.execute(
            "SELECT * FROM search_posts WHERE reddit_url = ? LIMIT 1",
            (reddit_url,)
        ).fetchone()
        if row:
            return "search_post", dict(row)
        return None, None

    def find_post_by_reddit_post_id(self, reddit_post_id):
        """Return (kind, post_dict) for a Reddit post by its short post ID
        (the alphanumeric segment between `/comments/` and the slug).

        URLs in `post_urls.reddit_url` and `search_posts.reddit_url` may
        be stored with the post's actual slug, while incoming URLs from
        the user's sheet may use Reddit's `/comment/` placeholder slug
        (or no slug at all). The post ID itself is immutable — Reddit
        URLs always contain `/comments/<post_id>/`. Matching on that
        substring is robust to every slug variant.

        Legacy single-table caller. New code should prefer
        `find_posts_by_reddit_post_id` (plural), which surfaces matches
        in BOTH the legacy `posts` and `search_posts` tables — needed
        for bulk deploy when a Reddit post has been imported into
        both pipelines.
        """
        rows = self.find_posts_by_reddit_post_id(reddit_post_id)
        return rows[0] if rows else (None, None)

    def find_posts_by_reddit_post_id(self, reddit_post_id):
        """Return EVERY (kind, post_dict) matching the given Reddit post
        ID — both `posts` (kind="post") and `search_posts`
        (kind="search_post"). Empty list if neither table has it.

        Used by bulk deploy to consider candidate comments from BOTH
        pipelines when a Reddit post appears in both. Without this,
        the user's `search_comments` rows are invisible whenever a
        legacy `posts` row happens to also exist for the same Reddit
        post.
        """
        if not reddit_post_id:
            return []
        like_a = f"%/comments/{reddit_post_id}/%"
        like_b = f"%/comments/{reddit_post_id}"
        out = []
        row = self.conn.execute(
            """SELECT p.*, pu.reddit_url
               FROM post_urls pu JOIN posts p ON pu.post_id = p.id
               WHERE pu.reddit_url LIKE ? OR pu.reddit_url LIKE ?
               LIMIT 1""",
            (like_a, like_b)
        ).fetchone()
        if row:
            out.append(("post", dict(row)))
        row = self.conn.execute(
            """SELECT * FROM search_posts
               WHERE reddit_url LIKE ? OR reddit_url LIKE ?
               LIMIT 1""",
            (like_a, like_b)
        ).fetchone()
        if row:
            out.append(("search_post", dict(row)))
        return out

    def find_undeployed_comments_for_post(self, post_id, kind):
        """Return all rows for the given post that are NOT yet deployed —
        the candidate pool for Tier-2 body fuzzy matching in bulk deploy.

        `kind` is "comment" (queries `comments`/post_id) or
        "search_comment" (queries `search_comments`/search_post_id).
        We deliberately include 'removed' and 'replace' rows: a row may
        have been auto-marked dead by the live-check job before the user
        ran bulk-deploy, but the user is now telling us "no, it was
        actually deployed here". Letting bulk-deploy match those rows
        and restore them mirrors the existing restore_to_deployed path.

        We EXCLUDE 'report' (and 'deployed' / 'paid' / 'archived')
        because those are user-driven terminal states. Body-matching
        a 'report' row into the candidate pool would let a sheet re-
        upload silently flip it back to 'deployed' — the bulk-deploy
        rollback bug that prompted this guard.
        """
        if kind == "comment":
            rows = self.conn.execute(
                """SELECT id, body, status, reddit_comment_url, brand_id,
                          account_id
                   FROM comments
                   WHERE post_id = ?
                     AND status NOT IN ('deployed', 'paid', 'archived', 'report')""",
                (post_id,)
            ).fetchall()
        elif kind == "search_comment":
            rows = self.conn.execute(
                """SELECT id, body, status, reddit_comment_url, brand_id,
                          account_id
                   FROM search_comments
                   WHERE search_post_id = ?
                     AND status NOT IN ('deployed', 'paid', 'archived', 'report')""",
                (post_id,)
            ).fetchall()
        else:
            return []
        return [dict(r) for r in rows]

    def deploy_post(self, post_id, kind, reddit_url, subreddit_id=None,
                    deployed_at=None):
        """Mark a post row deployed and ensure its Reddit URL is on file.

        `kind` is "post" (legacy → updates posts.status, ensures
        post_urls row) or "search_post" (updates search_posts.status;
        URL is already on the row by definition).

        Idempotent: deploying an already-deployed post is a no-op.
        """
        if not deployed_at:
            deployed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if kind == "post":
            row = self.conn.execute(
                "SELECT status, subreddit_id FROM posts WHERE id = ?",
                (post_id,)
            ).fetchone()
            if not row:
                return False
            if row["status"] == "deployed":
                return False  # idempotent skip
            sid = subreddit_id or row["subreddit_id"]
            # Ensure the URL is linked. link_url_to_post handles
            # replace-existing semantics.
            self.link_url_to_post(post_id, reddit_url, sid)
            self.conn.execute(
                "UPDATE posts SET status = 'deployed' WHERE id = ?",
                (post_id,)
            )
            self.conn.commit()
            return True
        if kind == "search_post":
            row = self.conn.execute(
                "SELECT status FROM search_posts WHERE id = ?", (post_id,)
            ).fetchone()
            if not row:
                return False
            if row["status"] == "deployed":
                return False
            self.conn.execute(
                "UPDATE search_posts SET status = 'deployed' WHERE id = ?",
                (post_id,)
            )
            self.conn.commit()
            return True
        return False

    def update_comment_url(self, comment_id, url):
        self.conn.execute("UPDATE comments SET reddit_comment_url = ? WHERE id = ?", (url, comment_id))
        self.conn.commit()

    def update_search_comment_url(self, comment_id, url):
        self.conn.execute("UPDATE search_comments SET reddit_comment_url = ? WHERE id = ?", (url, comment_id))
        self.conn.commit()

    def undeploy_comment(self, comment_id):
        """Revert a deployed comment back to assigned status."""
        self.conn.execute(
            """UPDATE comments SET status = 'assigned', deployed_at = NULL,
               reddit_comment_url = NULL, paid_at = NULL
               WHERE id = ? AND status = 'deployed'""",
            (comment_id,)
        )
        self.conn.commit()

    def inform_comment(self, comment_id):
        self.conn.execute(
            "UPDATE comments SET status = 'informed', prev_status = 'assigned', informed_at = datetime('now') WHERE id = ? AND status = 'assigned'",
            (comment_id,)
        )
        self.conn.commit()

    def mark_comment_deleted(self, comment_id):
        deleted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "UPDATE comments SET status = 'deleted', deleted_at = ?, paid_at = NULL WHERE id = ?",
            (deleted_at, comment_id)
        )
        self.conn.commit()

    def mark_comment_removed(self, comment_id):
        """Mark a comment as removed/deleted on Reddit (allowed for any status).

        Manual / forced path — always sets status = 'removed' regardless
        of how recently the comment was published. The auto-detection
        path (Check Live, bulk-deploy verification) should use
        `mark_comment_removed_or_replace` instead so the 14-day
        'replace' window can apply.
        """
        row = self.conn.execute("SELECT status FROM comments WHERE id = ?", (comment_id,)).fetchone()
        old_status = row["status"] if row else None
        if old_status == 'removed':
            return
        self.conn.execute(
            "UPDATE comments SET status = 'removed', prev_status = ?, paid_at = NULL WHERE id = ?",
            (old_status, comment_id)
        )
        self.conn.commit()

    # --- 'replace' window helpers --------------------------------------
    #
    # `replace` is a sub-state of `removed` produced ONLY by the
    # auto-detection paths (Check Live, bulk-deploy verification) when
    # the Reddit publish timestamp (`posted_at`) is within the last
    # REPLACE_WINDOW_DAYS days. It signals "removed recently — still
    # eligible to redeploy under another account" without burying the
    # row in the regular Removed bucket. For dashboard / KPI counting
    # `replace` rolls up under Removed (see
    # `get_report_months_for_client` / `get_report_aggregate_for_client`).

    REPLACE_WINDOW_DAYS = 14

    def _choose_removed_status(self, comment_id, days=None):
        """Pick 'replace' vs 'removed' for an auto-detected removal.

        Anchor is preferred in this order:
          1. `posted_at`  — real Reddit publish timestamp (created_utc).
          2. `deployed_at` — bot bookkeeping time. Usually within
             minutes/hours of posted_at; falling back here lets the
             14-day rule still fire when Reddit can't return a
             timestamp (rate-limit, comment fully scrubbed from the
             tree, etc.).

        Returns 'replace' if the chosen anchor is within `days` days
        of now, else 'removed'. If both anchors are NULL/unparseable
        we default to 'removed' — the safe, conservative pick.
        """
        if days is None:
            days = self.REPLACE_WINDOW_DAYS
        row = self.conn.execute(
            "SELECT posted_at, deployed_at FROM comments WHERE id = ?",
            (comment_id,)
        ).fetchone()
        if not row:
            return "removed"
        anchor = row["posted_at"] or row["deployed_at"]
        if not anchor:
            return "removed"
        try:
            s = str(anchor).replace("T", " ").split(".")[0].strip()
            ts = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return "removed"
        delta = datetime.utcnow() - ts
        if delta.total_seconds() < 0:
            return "replace"
        if delta.days < days:
            return "replace"
        return "removed"

    def mark_comment_removed_or_replace(self, comment_id, posted_at_hint=None,
                                        days=None):
        """Auto-detection variant of `mark_comment_removed`.

        Writes `posted_at_hint` first (if provided and the row's
        posted_at is empty), then picks 'replace' or 'removed' via
        `_choose_removed_status` and updates the row. Preserves
        `prev_status` and clears `paid_at` to mirror
        `mark_comment_removed`.

        Returns the chosen status ('replace' | 'removed'), or None if
        the row didn't exist. Idempotent: a row already in 'removed'
        or 'replace' is left alone (the rule is one-shot at detection
        time; later re-detections shouldn't move 'replace' → 'removed'
        as the 14-day window slides past).
        """
        row = self.conn.execute(
            "SELECT status, posted_at FROM comments WHERE id = ?",
            (comment_id,)
        ).fetchone()
        if not row:
            return None
        old_status = row["status"]
        if old_status in ("removed", "replace"):
            return old_status
        if posted_at_hint and not row["posted_at"]:
            self.conn.execute(
                "UPDATE comments SET posted_at = ? WHERE id = ?",
                (posted_at_hint, comment_id)
            )
            # No commit yet — the status flip below shares the txn.
        new_status = self._choose_removed_status(comment_id, days=days)
        self.conn.execute(
            "UPDATE comments SET status = ?, prev_status = ?, paid_at = NULL WHERE id = ?",
            (new_status, old_status, comment_id)
        )
        self.conn.commit()
        return new_status

    def mark_removed_with_url(self, comment_id, kind, reddit_url, posted_at=None):
        """Attach a Reddit URL to a comment row AND mark it removed.

        Used by Bulk Deploy when the URL points at a comment that
        Reddit has since removed/deleted — the user still wants the
        URL recorded against the row, but the bot should reflect the
        actual Reddit state. Idempotent: returns the *previous* status
        (for logging) and is a no-op if the row is already removed.

        `kind` is "comment" or "search_comment".
        Returns the previous status string, or None if the row wasn't
        found.
        """
        if not comment_id or not reddit_url:
            return None
        if kind == "comment":
            row = self.conn.execute(
                "SELECT status FROM comments WHERE id = ?", (comment_id,)
            ).fetchone()
            if not row:
                return None
            old_status = row["status"]
            if old_status == "removed":
                # Still patch the URL if the row didn't have one,
                # but no status flip needed.
                self.conn.execute(
                    """UPDATE comments
                       SET reddit_comment_url = COALESCE(?, reddit_comment_url),
                           posted_at = COALESCE(posted_at, ?)
                       WHERE id = ?""",
                    (reddit_url, posted_at, comment_id)
                )
                self.conn.commit()
                return old_status
            # Write URL + posted_at first so the chooser sees the
            # freshly-supplied timestamp when deciding 'replace' vs
            # 'removed'. We then re-update with the chosen status.
            self.conn.execute(
                """UPDATE comments
                   SET reddit_comment_url = ?,
                       posted_at = COALESCE(?, posted_at)
                   WHERE id = ?""",
                (reddit_url, posted_at, comment_id)
            )
            new_status = self._choose_removed_status(comment_id)
            self.conn.execute(
                """UPDATE comments
                   SET status = ?, prev_status = ?,
                       paid_at = NULL
                   WHERE id = ?""",
                (new_status, old_status, comment_id)
            )
            self.conn.commit()
            return old_status
        if kind == "search_comment":
            row = self.conn.execute(
                "SELECT status FROM search_comments WHERE id = ?", (comment_id,)
            ).fetchone()
            if not row:
                return None
            old_status = row["status"]
            if old_status == "removed":
                self.conn.execute(
                    """UPDATE search_comments
                       SET reddit_comment_url = COALESCE(?, reddit_comment_url),
                           posted_at = COALESCE(posted_at, ?)
                       WHERE id = ?""",
                    (reddit_url, posted_at, comment_id)
                )
                self.conn.commit()
                return old_status
            # Write URL + posted_at first so the chooser sees the
            # freshly-supplied timestamp before deciding 'replace'
            # vs 'removed'. Mirrors the `comments` branch above.
            self.conn.execute(
                """UPDATE search_comments
                   SET reddit_comment_url = ?,
                       posted_at = COALESCE(?, posted_at)
                   WHERE id = ?""",
                (reddit_url, posted_at, comment_id)
            )
            new_status = self._choose_removed_status_search(comment_id)
            self.conn.execute(
                """UPDATE search_comments
                   SET status = ?, prev_status = ?,
                       deleted_at = datetime('now'),
                       paid_at = NULL
                   WHERE id = ?""",
                (new_status, old_status, comment_id)
            )
            self.conn.commit()
            return old_status
        return None

    def unremove_comment(self, comment_id):
        """Revert a removed-or-replace comment back to its previous
        status (fallback to deployed). Accepts both 'removed' and
        'replace' as undo-able starting states — 'replace' is just a
        sub-state of removed.
        """
        row = self.conn.execute(
            "SELECT prev_status FROM comments WHERE id = ? "
            "AND status IN ('removed', 'replace')",
            (comment_id,)
        ).fetchone()
        if not row:
            return None
        restore_to = row["prev_status"] if row["prev_status"] else 'deployed'
        self.conn.execute(
            "UPDATE comments SET status = ?, prev_status = NULL "
            "WHERE id = ? AND status IN ('removed', 'replace')",
            (restore_to, comment_id)
        )
        self.conn.commit()
        return restore_to

    def undo_comment_status(self, comment_id):
        """Revert a comment to its previous status. Returns the restored status or None."""
        row = self.conn.execute(
            "SELECT prev_status FROM comments WHERE id = ?", (comment_id,)
        ).fetchone()
        if not row or not row["prev_status"]:
            return None
        prev = row["prev_status"]
        self.conn.execute(
            "UPDATE comments SET status = ?, prev_status = NULL WHERE id = ?",
            (prev, comment_id))
        self.conn.commit()
        return prev

    # --- Keyword Matching ---

    def update_matched_keywords(self, comment_id, keywords_json):
        self.conn.execute(
            "UPDATE comments SET matched_keywords = ? WHERE id = ?",
            (keywords_json, comment_id)
        )
        self.conn.commit()

    def detect_matched_keywords(self, comment_id):
        """Parse comment body against brand keywords. Returns matched list."""
        comment = self.get_comment(comment_id)
        if not comment or not comment.get("mentions_brand"):
            return []
        brand = self.get_brand(comment["brand_id"]) if comment.get("brand_id") else None
        if not brand:
            return []
        try:
            keywords = json.loads(brand.get("keywords", "[]"))
        except (json.JSONDecodeError, TypeError):
            return []
        if not keywords:
            return []
        body = comment["body"]
        matched = []
        for kw in keywords:
            if re.search(r'\b' + re.escape(kw) + r'\b', body, re.IGNORECASE):
                matched.append(kw)
        self.update_matched_keywords(comment_id, json.dumps(matched))
        return matched

    def backfill_matched_keywords(self, subreddit_id=None):
        """Batch-detect keywords for all brand-mention comments missing keywords."""
        query = """SELECT c.id FROM comments c
                   JOIN posts p ON c.post_id = p.id
                   WHERE c.mentions_brand = 1 AND (c.matched_keywords IS NULL OR c.matched_keywords = '')"""
        params = []
        if subreddit_id:
            query += " AND p.subreddit_id = ?"
            params.append(subreddit_id)
        rows = self.conn.execute(query, params).fetchall()
        count = 0
        for r in rows:
            matched = self.detect_matched_keywords(r["id"])
            if matched:
                count += 1
        return {"processed": len(rows), "with_keywords": count}

    # --- Filtered Comment Queries ---

    def get_filtered_comments(self, subreddit_id, status=None, mentions_brand=None, account_id=None, brand_id=None, sort_by=None):
        """Get comments with post info, filtered by status/brand/account.
        Includes both regular comments and search_comments for this subreddit."""
        # Regular comments
        q1 = """SELECT c.id, c.body, c.status, c.account_id, c.brand_id,
                       c.is_reply, c.mentions_brand, c.created_at, c.deployed_at,
                       c.paid_at, c.reddit_comment_url, c.comment_type,
                       c.suggested_post_day, c.suggested_order,
                       c.is_ours, c.matched_keywords, c.prev_status,
                       'comment' as source,
                       p.title as post_title, p.id as p_id,
                       (SELECT pu.reddit_url FROM post_urls pu WHERE pu.post_id = p.id LIMIT 1) as post_reddit_url
                FROM comments c
                JOIN posts p ON c.post_id = p.id
                WHERE p.subreddit_id = ?"""
        p1 = [subreddit_id]
        if status:
            q1 += " AND c.status = ?"
            p1.append(status)
        else:
            q1 += " AND c.status != 'deleted'"
        if mentions_brand is not None:
            q1 += " AND c.mentions_brand = ?"
            p1.append(1 if mentions_brand else 0)
        if account_id:
            q1 += " AND c.account_id = ?"
            p1.append(account_id)
        if brand_id:
            q1 += " AND c.brand_id = ?"
            p1.append(brand_id)

        # Search comments for this subreddit (match by subreddit name)
        sub_name_row = self.conn.execute("SELECT name FROM subreddits WHERE id = ?", (subreddit_id,)).fetchone()
        sub_name = sub_name_row["name"] if sub_name_row else ""

        q2 = """SELECT sc.id, sc.body, sc.status, sc.account_id, sc.brand_id,
                       sc.is_reply, sc.mentions_brand, sc.created_at, sc.deployed_at,
                       sc.paid_at, sc.reddit_comment_url, NULL as comment_type,
                       0 as suggested_post_day, 0 as suggested_order,
                       1 as is_ours, NULL as matched_keywords, sc.prev_status,
                       'search_comment' as source,
                       sp.title as post_title, sp.id as p_id,
                       sp.reddit_url as post_reddit_url
                FROM search_comments sc
                JOIN search_posts sp ON sc.search_post_id = sp.id
                WHERE LOWER(sp.subreddit) = LOWER(?)"""
        p2 = [sub_name]
        if status:
            q2 += " AND sc.status = ?"
            p2.append(status)
        else:
            q2 += " AND sc.status != 'deleted'"
        if mentions_brand is not None:
            q2 += " AND sc.mentions_brand = ?"
            p2.append(1 if mentions_brand else 0)
        if account_id:
            q2 += " AND sc.account_id = ?"
            p2.append(account_id)
        if brand_id:
            q2 += " AND sc.brand_id = ?"
            p2.append(brand_id)

        if sort_by == 'deployed_at':
            order = "ORDER BY deployed_at DESC, id DESC"
        else:
            order = "ORDER BY suggested_post_day, suggested_order, id"

        query = f"SELECT * FROM ({q1} UNION ALL {q2}) combined {order}"
        rows = self.conn.execute(query, p1 + p2).fetchall()
        return [dict(r) for r in rows]

    def get_all_comments_by_brand(self, brand_id, status=None, sort_by=None, live=False):
        """Get all comments (regular + search) for a brand across all subreddits.

        `live` controls Live Subreddits filtering on the regular comments table:
        False (default) excludes live, True returns only live, None returns both.
        Search comments live in their own pipeline so this flag doesn't affect them.
        """
        status_filter_reg = "AND c.status = ?" if status else "AND c.status != 'deleted'"
        status_filter_sc = "AND sc.status = ?" if status else "AND sc.status != 'deleted'"
        if live is False:
            status_filter_reg += " AND COALESCE(p.is_live, 0) = 0"
        elif live is True:
            status_filter_reg += " AND COALESCE(p.is_live, 0) = 1"

        q1 = f"""SELECT c.id, c.body, c.status, c.account_id, c.brand_id,
                        c.is_reply, c.mentions_brand, c.created_at, c.deployed_at,
                        c.paid_at, c.reddit_comment_url, c.comment_type,
                        c.suggested_post_day, c.suggested_order,
                        c.is_ours, c.matched_keywords, c.prev_status,
                        'comment' as source,
                        p.title as post_title, p.id as p_id,
                        s.name as subreddit_name,
                        (SELECT pu.reddit_url FROM post_urls pu WHERE pu.post_id = p.id LIMIT 1) as post_reddit_url
                 FROM comments c
                 JOIN posts p ON c.post_id = p.id
                 LEFT JOIN subreddits s ON p.subreddit_id = s.id
                 WHERE (c.brand_id = ? OR (c.brand_id IS NULL AND p.id IN (SELECT post_id FROM post_brands WHERE brand_id = ?))) {status_filter_reg}"""
        p1 = [brand_id, brand_id]
        if status:
            p1.append(status)

        q2 = f"""SELECT sc.id, sc.body, sc.status, sc.account_id, sc.brand_id,
                        sc.is_reply, sc.mentions_brand, sc.created_at, sc.deployed_at,
                        sc.paid_at, sc.reddit_comment_url, NULL as comment_type,
                        0 as suggested_post_day, 0 as suggested_order,
                        1 as is_ours, NULL as matched_keywords, sc.prev_status,
                        'search_comment' as source,
                        sp.title as post_title, sp.id as p_id,
                        sp.subreddit as subreddit_name,
                        sp.reddit_url as post_reddit_url
                 FROM search_comments sc
                 JOIN search_posts sp ON sc.search_post_id = sp.id
                 WHERE sc.brand_id = ? {status_filter_sc}"""
        p2 = [brand_id]
        if status:
            p2.append(status)

        if sort_by == 'deployed_at':
            order = "ORDER BY deployed_at DESC, id DESC"
        else:
            order = "ORDER BY created_at DESC, id DESC"

        # When live=True the user is on the Live Subreddits Comments view; search
        # comments aren't part of that flow, so suppress them.
        if live is True:
            query = f"SELECT * FROM ({q1}) combined {order}"
            rows = self.conn.execute(query, p1).fetchall()
        else:
            query = f"SELECT * FROM ({q1} UNION ALL {q2}) combined {order}"
            rows = self.conn.execute(query, p1 + p2).fetchall()
        return [dict(r) for r in rows]

    def get_removed_comments(self, brand_id=None, subreddit_id=None):
        """All comments (regular + search) currently marked removed / replace, with their
        brand + subreddit + account, for the Check Live → Analyse view. `replace` =
        removed on Reddit but still inside the 14-day re-deploy window. No pagination —
        the removed set is a small subset and analytics needs the full list."""
        w1 = ["c.status IN ('removed','replace')"]
        w2 = ["sc.status IN ('removed','replace')"]
        p1, p2 = [], []
        if brand_id:
            w1.append("(c.brand_id = ? OR (c.brand_id IS NULL AND p.id IN "
                      "(SELECT post_id FROM post_brands WHERE brand_id = ?)))")
            p1 += [brand_id, brand_id]
            w2.append("sc.brand_id = ?"); p2.append(brand_id)
        if subreddit_id:
            w1.append("p.subreddit_id = ?"); p1.append(subreddit_id)
            row = self.conn.execute("SELECT name FROM subreddits WHERE id = ?", (subreddit_id,)).fetchone()
            w2.append("LOWER(sp.subreddit) = LOWER(?)"); p2.append(row["name"] if row else "")
        where1 = " AND ".join(w1)
        where2 = " AND ".join(w2)
        q1 = f"""SELECT c.id, 'comment' AS source, c.status, c.prev_status,
                        c.account_id, c.comment_type, c.reddit_comment_url,
                        c.posted_at, c.deployed_at, c.last_live_check, c.created_at,
                        p.title AS post_title, s.name AS subreddit_name, b.name AS brand_name
                 FROM comments c
                 JOIN posts p ON c.post_id = p.id
                 LEFT JOIN subreddits s ON p.subreddit_id = s.id
                 LEFT JOIN brands b ON c.brand_id = b.id
                 WHERE {where1}"""
        q2 = f"""SELECT sc.id, 'search_comment' AS source, sc.status, sc.prev_status,
                        sc.account_id, NULL AS comment_type, sc.reddit_comment_url,
                        sc.posted_at, sc.deployed_at, sc.last_live_check, sc.created_at,
                        sp.title AS post_title, sp.subreddit AS subreddit_name, b.name AS brand_name
                 FROM search_comments sc
                 JOIN search_posts sp ON sc.search_post_id = sp.id
                 LEFT JOIN brands b ON sc.brand_id = b.id
                 WHERE {where2}"""
        query = (f"SELECT * FROM ({q1} UNION ALL {q2}) combined "
                 "ORDER BY COALESCE(last_live_check, deployed_at, created_at) DESC, id DESC")
        rows = self.conn.execute(query, p1 + p2).fetchall()
        return [dict(r) for r in rows]

    def get_check_live_items_by_filter(self, brand_id=None, month=None, status=None):
        """Comments (both tables) matching optional brand_id / report_month / status, restricted
        to rows that HAVE a Reddit URL — shaped for `_build_check_live_items`. Powers the Settings
        'targeted status check' tool. The URL restriction is mandatory: `_check_live_batch` would
        flip a no-URL comment to 'deleted', so only checkable (posted) rows are returned. With no
        `status`, returns every non-deleted/archived row (which, after the URL filter, is the
        deployed/paid/report/removed/replace/replaced set)."""
        w1 = ["TRIM(COALESCE(c.reddit_comment_url, '')) != ''"]; p1 = []
        w2 = ["TRIM(COALESCE(sc.reddit_comment_url, '')) != ''"]; p2 = []
        if status:
            w1.append("c.status = ?"); p1.append(status)
            w2.append("sc.status = ?"); p2.append(status)
        else:
            w1.append("c.status NOT IN ('deleted','archived')")
            w2.append("sc.status NOT IN ('deleted','archived')")
        if month:
            w1.append("c.report_month = ?"); p1.append(month)
            w2.append("sc.report_month = ?"); p2.append(month)
        if brand_id:
            w1.append("(c.brand_id = ? OR (c.brand_id IS NULL AND p.brand_id = ?) "
                      "OR (c.brand_id IS NULL AND p.id IN (SELECT post_id FROM post_brands WHERE brand_id = ?)))")
            p1 += [brand_id, brand_id, brand_id]
            w2.append("(sc.brand_id = ? OR (sc.brand_id IS NULL AND sp.brand_id = ?))")
            p2 += [brand_id, brand_id]
        where1 = " AND ".join(w1); where2 = " AND ".join(w2)
        q1 = f"""SELECT c.id, 'comment' AS source, c.status, c.reddit_comment_url,
                        c.account_id, s.name AS subreddit_name,
                        COALESCE(b.name, pb.name) AS brand_name
                 FROM comments c
                 JOIN posts p ON c.post_id = p.id
                 LEFT JOIN subreddits s ON p.subreddit_id = s.id
                 LEFT JOIN brands b ON c.brand_id = b.id
                 LEFT JOIN brands pb ON p.brand_id = pb.id
                 WHERE {where1}"""
        q2 = f"""SELECT sc.id, 'search_comment' AS source, sc.status, sc.reddit_comment_url,
                        sc.account_id, sp.subreddit AS subreddit_name, b.name AS brand_name
                 FROM search_comments sc
                 JOIN search_posts sp ON sc.search_post_id = sp.id
                 LEFT JOIN brands b ON sc.brand_id = b.id
                 WHERE {where2}"""
        rows = self.conn.execute(f"{q1} UNION ALL {q2}", p1 + p2).fetchall()
        return [dict(r) for r in rows]

    def get_live_comment_counts(self, brand_id=None, subreddit_id=None):
        """Counts of comments currently LIVE on Reddit (status in deployed/paid/report/
        replaced) across comments + search_comments — the denominator for the Check Live →
        Analyse removal-rate view. Returns {total, by_brand, by_subreddit, by_account}
        (null/empty bucket to '—', matching the removed breakdown). Counts only."""
        live = ('deployed', 'paid', 'report', 'replaced')
        ph_live = ",".join("?" * len(live))
        w1 = [f"c.status IN ({ph_live})"]
        w2 = [f"sc.status IN ({ph_live})"]
        p1, p2 = list(live), list(live)
        if brand_id:
            w1.append("(c.brand_id = ? OR (c.brand_id IS NULL AND p.id IN "
                      "(SELECT post_id FROM post_brands WHERE brand_id = ?)))")
            p1 += [brand_id, brand_id]
            w2.append("sc.brand_id = ?"); p2.append(brand_id)
        if subreddit_id:
            w1.append("p.subreddit_id = ?"); p1.append(subreddit_id)
            row = self.conn.execute("SELECT name FROM subreddits WHERE id = ?", (subreddit_id,)).fetchone()
            w2.append("LOWER(sp.subreddit) = LOWER(?)"); p2.append(row["name"] if row else "")
        q1 = f"""SELECT 'comment' AS source, c.account_id AS account_id,
                        s.name AS subreddit_name, b.name AS brand_name
                 FROM comments c
                 JOIN posts p ON c.post_id = p.id
                 LEFT JOIN subreddits s ON p.subreddit_id = s.id
                 LEFT JOIN brands b ON c.brand_id = b.id
                 WHERE {' AND '.join(w1)}"""
        q2 = f"""SELECT 'search_comment' AS source, sc.account_id AS account_id,
                        sp.subreddit AS subreddit_name, b.name AS brand_name
                 FROM search_comments sc
                 JOIN search_posts sp ON sc.search_post_id = sp.id
                 LEFT JOIN brands b ON sc.brand_id = b.id
                 WHERE {' AND '.join(w2)}"""
        rows = self.conn.execute(f"{q1} UNION ALL {q2}", p1 + p2).fetchall()
        total = 0
        by_brand, by_subreddit, by_account = {}, {}, {}
        for r in rows:
            total += 1
            bk = (r["brand_name"] or "").strip() or "—"
            sk = (r["subreddit_name"] or "").strip() or "—"
            ak = (r["account_id"] or "").strip() or "—"
            by_brand[bk] = by_brand.get(bk, 0) + 1
            by_subreddit[sk] = by_subreddit.get(sk, 0) + 1
            by_account[ak] = by_account.get(ak, 0) + 1
        return {"total": total, "by_brand": by_brand,
                "by_subreddit": by_subreddit, "by_account": by_account}

    def get_all_comments_global(self, status=None, brand_id=None, subreddit_id=None,
                                account_id=None, sort_by=None, source=None,
                                date=None, limit=200, offset=0, live=False):
        """Get all comments (regular + search) globally with optional filters and pagination.

        `live` controls Live Subreddits filtering (regular `comments` table only;
        `search_comments` is unaffected since it lives in its own pipeline):
        - False (default): exclude comments on live posts.
        - True: only comments on live posts.
        - None: both.
        """
        # Build WHERE clauses dynamically
        w1, w2 = ["c.status NOT IN ('deleted','archived')"], ["sc.status NOT IN ('deleted','archived')"]
        p1, p2 = [], []
        if live is False:
            w1.append("COALESCE(p.is_live, 0) = 0")
        elif live is True:
            w1.append("COALESCE(p.is_live, 0) = 1")

        if status:
            w1 = ["c.status = ?"]; p1.append(status)
            w2 = ["sc.status = ?"]; p2.append(status)
        if brand_id:
            # Comments side: match comments tagged with this brand_id, OR
            # organic comments (brand_id NULL) on a post associated with this
            # brand via the post_brands junction. Without this, "organic"
            # comments saved with brand_id=NULL by generate_comment_tree are
            # invisible when a brand filter is applied — even though they
            # belong to the brand's post.
            w1.append("(c.brand_id = ? OR (c.brand_id IS NULL AND p.id IN (SELECT post_id FROM post_brands WHERE brand_id = ?)))")
            p1.append(brand_id); p1.append(brand_id)
            w2.append("sc.brand_id = ?"); p2.append(brand_id)
        if account_id:
            w1.append("c.account_id = ?"); p1.append(account_id)
            w2.append("sc.account_id = ?"); p2.append(account_id)

        # Subreddit filter: ID for regular, name for search
        if subreddit_id:
            w1.append("p.subreddit_id = ?"); p1.append(subreddit_id)
            row = self.conn.execute("SELECT name FROM subreddits WHERE id = ?", (subreddit_id,)).fetchone()
            sub_name = row["name"] if row else ""
            w2.append("LOWER(sp.subreddit) = LOWER(?)"); p2.append(sub_name)
        if date:
            date_expr1 = "DATE(CASE WHEN c.status = 'paid' THEN COALESCE(c.paid_at, c.deployed_at, c.created_at) ELSE COALESCE(c.deployed_at, c.created_at) END)"
            date_expr2 = "DATE(CASE WHEN sc.status = 'paid' THEN COALESCE(sc.paid_at, sc.deployed_at, sc.created_at) ELSE COALESCE(sc.deployed_at, sc.created_at) END)"
            w1.append(f"{date_expr1} = ?"); p1.append(date)
            w2.append(f"{date_expr2} = ?"); p2.append(date)

        where1 = " AND ".join(w1)
        where2 = " AND ".join(w2)

        q1 = f"""SELECT c.id, c.body, c.status, c.account_id, c.brand_id,
                        c.is_reply, c.mentions_brand, c.created_at, c.deployed_at,
                        c.posted_at,
                        c.paid_at, c.reddit_comment_url, c.comment_type,
                        c.parent_comment_id,
                        c.suggested_post_day, c.suggested_order,
                        c.is_ours, c.matched_keywords, c.assigned_at, c.informed_at,
                        c.last_live_check, c.prev_status,
                        c.focus_phrase, c.focus_hit,
                        c.report_month, c.report_added_at,
                        'comment' as source,
                        p.title as post_title, p.id as p_id,
                        s.name as subreddit_name, b.name as brand_name,
                        (SELECT pu.reddit_url FROM post_urls pu WHERE pu.post_id = p.id LIMIT 1) as post_reddit_url,
                        p.prompt_version as post_prompt_version,
                        p.post_number as post_number
                 FROM comments c
                 JOIN posts p ON c.post_id = p.id
                 LEFT JOIN subreddits s ON p.subreddit_id = s.id
                 LEFT JOIN brands b ON c.brand_id = b.id
                 WHERE {where1}"""

        q2 = f"""SELECT sc.id, sc.body, sc.status, sc.account_id, sc.brand_id,
                        sc.is_reply, sc.mentions_brand, sc.created_at, sc.deployed_at,
                        sc.posted_at,
                        sc.paid_at, sc.reddit_comment_url, NULL as comment_type,
                        NULL as parent_comment_id,
                        0 as suggested_post_day, 0 as suggested_order,
                        1 as is_ours, NULL as matched_keywords, sc.assigned_at, sc.informed_at,
                        sc.last_live_check, sc.prev_status,
                        NULL as focus_phrase, NULL as focus_hit,
                        sc.report_month, sc.report_added_at,
                        'search_comment' as source,
                        sp.title as post_title, sp.id as p_id,
                        sp.subreddit as subreddit_name, b.name as brand_name,
                        sp.reddit_url as post_reddit_url,
                        NULL as post_prompt_version,
                        NULL as post_number
                 FROM search_comments sc
                 JOIN search_posts sp ON sc.search_post_id = sp.id
                 LEFT JOIN brands b ON sc.brand_id = b.id
                 WHERE {where2}"""

        if sort_by == 'deployed_at':
            order = "ORDER BY deployed_at DESC, id DESC"
        elif sort_by == 'oldest':
            order = "ORDER BY created_at ASC, id ASC"
        else:
            order = "ORDER BY created_at DESC, id DESC"

        # Source filter: only one table if specified.
        # When live=True (Live Subreddits Comments view), suppress search_comments
        # entirely — search comments live in their own pipeline, not on live posts.
        if source == 'comment' or live is True:
            inner = q1
            all_params = p1
        elif source == 'search_comment':
            inner = q2
            all_params = p2
        else:
            inner = f"{q1} UNION ALL {q2}"
            all_params = p1 + p2

        union = f"SELECT * FROM ({inner}) combined {order}"

        # Count + status breakdown (across ALL matching rows, not just current page)
        count_query = f"SELECT COUNT(*) as cnt FROM ({inner}) combined"
        total = self.conn.execute(count_query, all_params).fetchone()["cnt"]

        counts_q = f"SELECT status, COUNT(*) as cnt FROM ({inner}) combined GROUP BY status"
        status_counts = {r["status"]: r["cnt"] for r in self.conn.execute(counts_q, all_params).fetchall()}

        paid_cnt = self.conn.execute(
            f"SELECT COUNT(*) as cnt FROM ({inner}) combined WHERE status = 'paid'", all_params
        ).fetchone()["cnt"]
        live_cnt = self.conn.execute(
            f"SELECT COUNT(*) as cnt FROM ({inner}) combined WHERE status='deployed' AND last_live_check IS NOT NULL", all_params
        ).fetchone()["cnt"]

        # Paginated results
        paginated = f"{union} LIMIT ? OFFSET ?"
        rows = self.conn.execute(paginated, all_params + [limit, offset]).fetchall()
        return {"items": [dict(r) for r in rows], "total": total,
                "status_counts": status_counts, "paid_count": paid_cnt, "live_count": live_cnt}

    def get_live_status_analytics(self):
        """Get brand-wise monthly breakdown of comment statuses (live/removed/deployed/total)."""
        q = """
            SELECT brand_name, month,
                   COUNT(*) as total,
                   SUM(CASE WHEN status = 'deployed' THEN 1 ELSE 0 END) as deployed,
                   SUM(CASE WHEN status = 'deployed' AND last_live_check IS NOT NULL THEN 1 ELSE 0 END) as live,
                   SUM(CASE WHEN status IN ('removed','deleted') THEN 1 ELSE 0 END) as removed,
                   SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) as paid
            FROM (
                SELECT b.name as brand_name,
                       COALESCE(strftime('%%Y-%%m', c.deployed_at), strftime('%%Y-%%m', c.created_at)) as month,
                       c.status, c.last_live_check, c.paid_at
                FROM comments c
                LEFT JOIN brands b ON c.brand_id = b.id
                WHERE c.reddit_comment_url IS NOT NULL
                UNION ALL
                SELECT b.name as brand_name,
                       COALESCE(strftime('%%Y-%%m', sc.deployed_at), strftime('%%Y-%%m', sc.created_at)) as month,
                       sc.status, sc.last_live_check, sc.paid_at
                FROM search_comments sc
                LEFT JOIN brands b ON sc.brand_id = b.id
                WHERE sc.reddit_comment_url IS NOT NULL
            ) combined
            WHERE month IS NOT NULL
            GROUP BY brand_name, month
            ORDER BY month DESC, brand_name
        """
        rows = self.conn.execute(q).fetchall()
        return [dict(r) for r in rows]

    def get_deployed_comments_by_brand(self, brand_id=None, brand_name=None):
        """Get all deployed comments for a brand, with post info."""
        query = """SELECT c.*, p.title as post_title, pu.reddit_url as post_reddit_url
                   FROM comments c
                   JOIN posts p ON c.post_id = p.id
                   LEFT JOIN post_urls pu ON pu.post_id = p.id
                   WHERE c.status = 'deployed'"""
        params = []
        if brand_id:
            query += " AND c.brand_id = ?"
            params.append(brand_id)
        elif brand_name:
            query += " AND c.brand_id IN (SELECT id FROM brands WHERE LOWER(name) = LOWER(?))"
            params.append(brand_name)
        query += " ORDER BY c.deployed_at DESC, c.id DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_all_brands(self):
        """Get all brands across all subreddits (includes standalone brands)."""
        rows = self.conn.execute(
            """SELECT b.*, s.name as subreddit_name FROM brands b
               LEFT JOIN subreddits s ON b.subreddit_id = s.id
               ORDER BY b.name"""
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Client reporting module — CRUD + lifecycle helpers
    # ------------------------------------------------------------------

    def create_client(self, name, password_hash, monthly_target=None, notes=None):
        cur = self.conn.execute(
            """INSERT INTO clients (name, password_hash, monthly_target, notes)
               VALUES (?, ?, ?, ?)""",
            (name, password_hash, monthly_target, notes)
        )
        self.conn.commit()
        return cur.lastrowid

    def update_client(self, client_id, *, name=None, monthly_target=None,
                      notes=None, password_hash=None):
        sets = []
        vals = []
        if name is not None:
            sets.append("name = ?"); vals.append(name)
        if monthly_target is not None:
            sets.append("monthly_target = ?"); vals.append(monthly_target)
        if notes is not None:
            sets.append("notes = ?"); vals.append(notes)
        if password_hash is not None:
            sets.append("password_hash = ?"); vals.append(password_hash)
        if not sets:
            return
        vals.append(client_id)
        self.conn.execute(
            f"UPDATE clients SET {', '.join(sets)} WHERE id = ?",
            vals
        )
        self.conn.commit()

    def delete_client(self, client_id):
        self.conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
        self.conn.commit()

    def get_client(self, client_id):
        row = self.conn.execute(
            "SELECT * FROM clients WHERE id = ?", (client_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["emails"] = [r["email"] for r in self.conn.execute(
            "SELECT email FROM client_emails WHERE client_id = ? ORDER BY is_primary DESC, email",
            (client_id,)
        ).fetchall()]
        d["primary_email"] = next(iter(
            (r["email"] for r in self.conn.execute(
                "SELECT email FROM client_emails WHERE client_id = ? AND is_primary = 1 LIMIT 1",
                (client_id,)
            ).fetchall())
        ), (d["emails"][0] if d["emails"] else None))
        d["brand_ids"] = [r["brand_id"] for r in self.conn.execute(
            "SELECT brand_id FROM client_brands WHERE client_id = ?",
            (client_id,)
        ).fetchall()]
        return d

    def get_client_by_email(self, email):
        """Look up the client by ANY of their associated emails (one
        client may have many; all share the same password)."""
        row = self.conn.execute(
            """SELECT c.* FROM clients c
               JOIN client_emails ce ON ce.client_id = c.id
               WHERE LOWER(ce.email) = LOWER(?)
               LIMIT 1""",
            (email,)
        ).fetchone()
        return dict(row) if row else None

    def list_clients(self):
        rows = self.conn.execute(
            """SELECT c.*,
                      (SELECT COUNT(*) FROM client_emails WHERE client_id = c.id) AS email_count,
                      (SELECT COUNT(*) FROM client_brands WHERE client_id = c.id) AS brand_count
               FROM clients c
               ORDER BY c.name COLLATE NOCASE"""
        ).fetchall()
        return [dict(r) for r in rows]

    def add_client_email(self, client_id, email):
        """Append a single email to a client. Lowercased, trimmed.
        Returns (True, None) on success or (False, reason) on failure.
        Reasons: 'invalid' (empty / no @), 'taken' (used by some
        other client), 'duplicate' (already on this client).
        """
        e = (email or "").strip().lower()
        if not e or "@" not in e or "." not in e:
            return (False, "invalid")
        # Cross-client uniqueness check.
        owner = self.conn.execute(
            "SELECT client_id FROM client_emails WHERE LOWER(email) = LOWER(?)",
            (e,)
        ).fetchone()
        if owner:
            if owner["client_id"] == client_id:
                return (False, "duplicate")
            return (False, "taken")
        # If the client has zero emails so far, this one becomes primary.
        existing = self.conn.execute(
            "SELECT COUNT(*) AS c FROM client_emails WHERE client_id = ?",
            (client_id,)
        ).fetchone()
        is_primary = 1 if (existing and existing["c"] == 0) else 0
        self.conn.execute(
            "INSERT INTO client_emails (client_id, email, is_primary) VALUES (?, ?, ?)",
            (client_id, e, is_primary)
        )
        self.conn.commit()
        return (True, None)

    def remove_client_email(self, client_id, email):
        """Delete one email from a client. Refuses to delete the last
        email on the account (the client would lose all sign-in routes).
        Returns (True, None) or (False, reason). Reasons: 'not_found',
        'last_email'.
        """
        e = (email or "").strip().lower()
        row = self.conn.execute(
            "SELECT id, is_primary FROM client_emails WHERE client_id = ? AND LOWER(email) = LOWER(?)",
            (client_id, e)
        ).fetchone()
        if not row:
            return (False, "not_found")
        total = self.conn.execute(
            "SELECT COUNT(*) AS c FROM client_emails WHERE client_id = ?",
            (client_id,)
        ).fetchone()["c"]
        if total <= 1:
            return (False, "last_email")
        self.conn.execute("DELETE FROM client_emails WHERE id = ?", (row["id"],))
        # If we dropped the primary, promote the next email alphabetically.
        if row["is_primary"]:
            promote = self.conn.execute(
                "SELECT id FROM client_emails WHERE client_id = ? ORDER BY email LIMIT 1",
                (client_id,)
            ).fetchone()
            if promote:
                self.conn.execute(
                    "UPDATE client_emails SET is_primary = 1 WHERE id = ?",
                    (promote["id"],)
                )
        self.conn.commit()
        return (True, None)

    def set_primary_email(self, client_id, email):
        """Flip the is_primary flag so exactly one row is primary."""
        e = (email or "").strip().lower()
        row = self.conn.execute(
            "SELECT id FROM client_emails WHERE client_id = ? AND LOWER(email) = LOWER(?)",
            (client_id, e)
        ).fetchone()
        if not row:
            return (False, "not_found")
        self.conn.execute(
            "UPDATE client_emails SET is_primary = 0 WHERE client_id = ?",
            (client_id,)
        )
        self.conn.execute(
            "UPDATE client_emails SET is_primary = 1 WHERE id = ?",
            (row["id"],)
        )
        self.conn.commit()
        return (True, None)

    def set_client_emails(self, client_id, emails):
        """Replace the client's email set. `emails` is a list of strings;
        the first one is flagged is_primary."""
        self.conn.execute("DELETE FROM client_emails WHERE client_id = ?", (client_id,))
        for i, e in enumerate(emails or []):
            e = (e or "").strip().lower()
            if not e:
                continue
            self.conn.execute(
                "INSERT OR IGNORE INTO client_emails (client_id, email, is_primary) VALUES (?, ?, ?)",
                (client_id, e, 1 if i == 0 else 0)
            )
        self.conn.commit()

    def set_client_brands(self, client_id, brand_ids):
        """Replace the client's brand associations. `brand_ids` is a list
        of brand IDs."""
        self.conn.execute("DELETE FROM client_brands WHERE client_id = ?", (client_id,))
        for bid in (brand_ids or []):
            try:
                bid_int = int(bid)
            except (TypeError, ValueError):
                continue
            self.conn.execute(
                "INSERT OR IGNORE INTO client_brands (client_id, brand_id) VALUES (?, ?)",
                (client_id, bid_int)
            )
        self.conn.commit()

    def touch_client_last_login(self, client_id):
        self.conn.execute(
            "UPDATE clients SET last_login_at = datetime('now') WHERE id = ?",
            (client_id,)
        )
        self.conn.commit()

    def client_brand_ids(self, client_id):
        """List of brand_ids a client can see. Used for row-level access
        control across every /portal/* query."""
        return [r["brand_id"] for r in self.conn.execute(
            "SELECT brand_id FROM client_brands WHERE client_id = ?",
            (client_id,)
        ).fetchall()]

    # --- Password reset tokens ---

    def create_password_reset_token(self, client_id, token_hash,
                                     expires_at, requested_email=None,
                                     requested_ip=None):
        """Insert a new reset-token row. `token_hash` is the SHA-256 of
        the raw token we email out — we never store the raw token in
        the DB. `expires_at` is an ISO datetime string (UTC).
        """
        self.conn.execute(
            """INSERT INTO password_reset_tokens
               (client_id, token_hash, expires_at, requested_email, requested_ip)
               VALUES (?, ?, ?, ?, ?)""",
            (client_id, token_hash, expires_at, requested_email, requested_ip)
        )
        self.conn.commit()

    def get_password_reset_by_token_hash(self, token_hash):
        """Look up an unconsumed, unexpired reset-token row by its hash.
        Returns dict or None. The portal calls this when the user clicks
        the reset link in their email; on a match it lets them set a
        new password and then calls `consume_password_reset_token`.
        """
        row = self.conn.execute(
            """SELECT * FROM password_reset_tokens
               WHERE token_hash = ?
                 AND consumed_at IS NULL
                 AND expires_at > datetime('now')
               LIMIT 1""",
            (token_hash,)
        ).fetchone()
        return dict(row) if row else None

    def consume_password_reset_token(self, token_hash):
        """Mark the token consumed so it can't be reused."""
        self.conn.execute(
            """UPDATE password_reset_tokens
               SET consumed_at = datetime('now')
               WHERE token_hash = ?""",
            (token_hash,)
        )
        self.conn.commit()

    def cleanup_expired_password_resets(self):
        """Best-effort cleanup of expired or consumed tokens older than
        one day. Safe to call periodically.
        """
        self.conn.execute(
            """DELETE FROM password_reset_tokens
               WHERE expires_at < datetime('now', '-1 day')
                  OR consumed_at < datetime('now', '-1 day')"""
        )
        self.conn.commit()

    # --- Report lifecycle helpers ---

    # Sentinel returned by move_comment_to_report when the row has no
    # brand_id and the caller didn't supply one. The API layer maps
    # this to a 422 with reason="brand_required" so the UI can prompt
    # for a brand and retry. Distinct from None (row missing / wrong
    # status) so callers don't confuse "needs brand" with "not found".
    BRAND_REQUIRED = object()

    def _resolve_post_brand(self, comment_id, source):
        """Return the parent post's brand for a comment, if it's
        unambiguous; otherwise None.

        Unambiguous means:
          - `comments`: `posts.brand_id` is set, OR exactly one row
            in `post_brands` for that post.
          - `search_comments`: `search_posts.brand_id` is set.

        Multi-brand posts and unbranded posts both return None —
        the caller (move_comment_to_report) treats both as
        "need a manual pick" and surfaces candidates separately.
        """
        if source == "comment":
            row = self.conn.execute(
                """SELECT p.brand_id FROM comments c
                     JOIN posts p ON c.post_id = p.id
                    WHERE c.id = ?""",
                (comment_id,)
            ).fetchone()
            if row and row["brand_id"] is not None:
                return int(row["brand_id"])
            # Multi-brand post case via junction.
            jr = self.conn.execute(
                """SELECT pb.brand_id FROM comments c
                     JOIN post_brands pb ON pb.post_id = c.post_id
                    WHERE c.id = ?""",
                (comment_id,)
            ).fetchall()
            if len(jr) == 1:
                return int(jr[0]["brand_id"])
            return None
        else:
            row = self.conn.execute(
                """SELECT sp.brand_id FROM search_comments sc
                     JOIN search_posts sp ON sc.search_post_id = sp.id
                    WHERE sc.id = ?""",
                (comment_id,)
            ).fetchone()
            if row and row["brand_id"] is not None:
                return int(row["brand_id"])
            return None

    def report_brand_candidates(self, comment_id, source):
        """When `move_comment_to_report` returns BRAND_REQUIRED,
        the API layer calls this to decide what brand picker the
        modal should show.

        Returns a list of `{id, name}` dicts:
          - For a multi-brand `comments` parent post → those
            specific brands (so the picker is scoped, not
            "everything in the system").
          - Otherwise (fully-orphaned post) → an empty list. The
            UI falls back to /api/brands/all in that case.
        """
        if source == "comment":
            rows = self.conn.execute(
                """SELECT b.id, b.name FROM comments c
                     JOIN post_brands pb ON pb.post_id = c.post_id
                     JOIN brands b ON b.id = pb.brand_id
                    WHERE c.id = ?
                    ORDER BY b.name""",
                (comment_id,)
            ).fetchall()
            return [{"id": r["id"], "name": r["name"]} for r in rows]
        return []

    def move_comment_to_report(self, comment_id, source, report_month,
                                actor_email=None, brand_id=None,
                                target_status='report', allowed_from=('deployed', 'paid')):
        """Flip a comment's status to `target_status` ('report' or 'replaced') and stamp
        the month. `allowed_from` = the statuses eligible for this outcome ('deployed'/
        'paid' for report; 'replace' for replaced). Both outcomes reuse the same
        attribution columns (report_month / report_added_at / prev_status / brand_id).

        Returns the previous status string on success, None if the
        row wasn't found / isn't in deployed-or-paid status, or the
        BRAND_REQUIRED sentinel if the row has no brand_id and we
        couldn't infer one from the parent post.

        Source: "comment" or "search_comment". Only rows currently
        in 'deployed' or 'paid' status are eligible.

        Brand resolution priority (mirrors the dashboard chain in
        `get_comments_for_client_month` — keep these two in sync):

          1. Caller-supplied `brand_id` (manual disambiguation).
          2. The row's own `brand_id`.
          3. Parent post's brand (single-brand) — for `comments`,
             `posts.brand_id`; for `search_comments`,
             `search_posts.brand_id`.
          4. `post_brands` junction — but only if exactly one
             brand. Multi-brand posts require manual pick.

        If none of those yields a brand → BRAND_REQUIRED. When the
        row has NULL `brand_id` and we resolve via 3 or 4 we
        silently set `comments.brand_id` so future dashboard
        queries can match it directly (no repeated JOIN chain).
        """
        if not report_month or not comment_id:
            return None
        table = "comments" if source == "comment" else "search_comments"
        row = self.conn.execute(
            f"SELECT status, report_month, brand_id FROM {table} WHERE id = ?",
            (comment_id,)
        ).fetchone()
        if not row:
            return None
        old_status = row["status"]
        if old_status not in tuple(allowed_from):
            return None
        effective_brand_id = row["brand_id"]
        if effective_brand_id is None:
            # Resolution chain. Caller override wins; then parent post.
            try:
                override = int(brand_id) if brand_id not in (None, '', 0) else None
            except (TypeError, ValueError):
                override = None
            resolved = override if override else self._resolve_post_brand(comment_id, source)
            if resolved is None:
                return self.BRAND_REQUIRED
            effective_brand_id = resolved
            self.conn.execute(
                f"UPDATE {table} SET brand_id = ? WHERE id = ?",
                (resolved, comment_id)
            )
        self.conn.execute(
            f"""UPDATE {table}
                SET status = ?,
                    prev_status = ?,
                    report_month = ?,
                    report_added_at = datetime('now')
                WHERE id = ?""",
            (target_status, old_status, report_month, comment_id)
        )
        self.conn.commit()
        self._log_report_audit(
            comment_id=comment_id, source=source, action="added",
            report_month=report_month, prev_month=row["report_month"],
            actor_email=actor_email,
        )
        return old_status

    def undo_report(self, comment_id, source, actor_email=None):
        """Reverse a 'report' status, restoring the comment to the live
        'deployed' pipeline state. Returns the new status or None on
        failure.

        Always restores to 'deployed' (never 'paid'): undoing a report
        means the comment is no longer in a monthly report, but it IS
        still a live deployed comment. The operator re-marks it 'paid'
        explicitly if it was paid for — we don't silently resurrect the
        prior 'paid' state, which previously left reverted rows stuck in
        a paid bucket the user didn't expect.
        """
        table = "comments" if source == "comment" else "search_comments"
        row = self.conn.execute(
            f"SELECT status, prev_status, report_month FROM {table} WHERE id = ?",
            (comment_id,)
        ).fetchone()
        if not row or row["status"] not in ("report", "replaced"):
            return None
        # Both 'report' and 'replaced' came from a live deployed/paid comment, so undo
        # returns them to the live 'deployed' pipeline.
        restore_to = "deployed"
        self.conn.execute(
            f"""UPDATE {table}
                SET status = ?, prev_status = NULL,
                    report_month = NULL, report_added_at = NULL
                WHERE id = ?""",
            (restore_to, comment_id)
        )
        self.conn.commit()
        self._log_report_audit(
            comment_id=comment_id, source=source, action="removed",
            report_month=None, prev_month=row["report_month"],
            actor_email=actor_email,
        )
        return restore_to

    def reclassify_report_tag(self, comment_id, source, to_status, actor_email=None):
        """Relabel an already-terminal report comment between the two parallel tags
        'report' <-> 'replaced'. Pure status flip: report_month / report_added_at /
        prev_status / brand_id are all preserved (it's NOT a new report, just a different
        tag on the same delivered comment).

        Eligible only from {'report','replaced'} and only when the target differs. Returns
        the previous status on success, None if the row is missing / not in a terminal
        report state / already at `to_status`. The live gate (only relabel comments still
        live on Reddit) lives in the endpoint, mirroring move_comment_to_report.
        """
        if to_status not in ('report', 'replaced') or not comment_id:
            return None
        table = "comments" if source == "comment" else "search_comments"
        row = self.conn.execute(
            f"SELECT status, report_month FROM {table} WHERE id = ?",
            (comment_id,)
        ).fetchone()
        if not row:
            return None
        old_status = row["status"]
        if old_status not in ('report', 'replaced') or old_status == to_status:
            return None
        self.conn.execute(
            f"UPDATE {table} SET status = ? WHERE id = ?",
            (to_status, comment_id)
        )
        self.conn.commit()
        self._log_report_audit(
            comment_id=comment_id, source=source, action="retagged",
            report_month=row["report_month"], prev_month=row["report_month"],
            actor_email=actor_email,
        )
        return old_status

    def _log_report_audit(self, *, comment_id, source, action,
                           report_month, prev_month, actor_email):
        try:
            self.conn.execute(
                """INSERT INTO report_audit
                   (comment_id, source, action, report_month, prev_month, actor_email)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (comment_id, source, action, report_month, prev_month, actor_email)
            )
            self.conn.commit()
        except Exception as e:
            print(f"[report_audit] insert failed: {e}", flush=True)

    def bulk_move_to_report(self, *, ids, report_month, actor_email=None,
                              brand_id_override=None,
                              target_status='report', allowed_from=('deployed', 'paid')):
        """`ids` is a list of {id, source} dicts. Each eligible row (status in
        `allowed_from`) is flipped to `target_status` ('report' or 'replaced'). Returns
        counts {updated, skipped, brand_required}.

        `brand_id_override` (optional): if supplied, applied to any
        row in the batch whose `brand_id` is currently NULL. Branded
        rows in the batch keep their existing brand untouched. Rows
        that lack a brand AND have no override skip with a bump in
        `brand_required` so the caller can surface the count.
        """
        if not ids or not report_month:
            return {"updated": 0, "skipped": 0, "brand_required": 0}
        updated = 0
        skipped = 0
        brand_required = 0
        for entry in ids:
            try:
                cid = int(entry.get("id"))
            except (TypeError, ValueError, AttributeError):
                skipped += 1; continue
            src = entry.get("source") or "comment"
            res = self.move_comment_to_report(
                cid, src, report_month, actor_email=actor_email,
                brand_id=brand_id_override,
                target_status=target_status, allowed_from=allowed_from,
            )
            if res is self.BRAND_REQUIRED:
                brand_required += 1
                skipped += 1
            elif res is not None:
                updated += 1
            else:
                skipped += 1
        return {"updated": updated, "skipped": skipped,
                "brand_required": brand_required}

    # --- Post-level report flow ---------------------------------------
    #
    # A "reported post" is one whose status='report'. We get there by
    # flipping the post AND its deployed/paid comments (including the
    # HQ root) so the dashboard's comment-level visibility query
    # surfaces the right rows. Undo reverses both.

    def move_post_to_report(self, post_id, *, report_month,
                             actor_email=None, brand_id_override=None,
                             comment_ids=None):
        """Flip a post to status='report'. Each reported post anchors
        exactly **one** comment on Reddit — the deployed HQ root (the
        cluster head that carries the brand mention).

        Behavior:
          - If a deployed HQ root exists under this post (comment_type=
            'hq', parent_comment_id IS NULL, reddit_comment_url set,
            status in deployed/paid), flip that single comment to
            'report'. Other comments under the post are left alone.
          - If no deployed HQ root exists, just flip the post —
            the dashboard's derived_status will mark it 'removed'
            because Mention Link is empty (= nothing to point at on
            Reddit).
          - `comment_ids` is accepted for backwards compatibility but
            ignored — the report always corresponds to at most the
            HQ root.

        Returns counts: {comments_updated, brand_required, post_flipped}.
        """
        if not report_month or not post_id:
            return {"comments_updated": 0, "brand_required": 0, "post_flipped": False}
        post_row = self.conn.execute(
            "SELECT id, status, prev_status, report_month FROM posts WHERE id = ?",
            (post_id,)
        ).fetchone()
        if not post_row:
            return {"comments_updated": 0, "brand_required": 0, "post_flipped": False}
        # Find the deployed HQ root. There's typically zero or one —
        # if more than one (unusual: multiple HQ threads under the
        # same post), pick the earliest-posted so the Mention Link
        # is stable.
        hq_row = self.conn.execute(
            """SELECT id FROM comments
                WHERE post_id = ?
                  AND comment_type = 'hq'
                  AND parent_comment_id IS NULL
                  AND TRIM(COALESCE(reddit_comment_url, '')) != ''
                  AND status IN ('deployed', 'paid')
                ORDER BY COALESCE(posted_at, deployed_at, created_at)
                LIMIT 1""",
            (post_id,)
        ).fetchone()
        eligible = {}  # id → marker row
        if hq_row:
            eligible[hq_row["id"]] = True
        # Flip each comment via the existing helper (handles brand
        # resolution + audit logging).
        comments_updated = 0
        brand_required = 0
        for cid in eligible:
            res = self.move_comment_to_report(
                cid, "comment", report_month,
                actor_email=actor_email, brand_id=brand_id_override,
            )
            if res is self.BRAND_REQUIRED:
                brand_required += 1
            elif res is not None:
                comments_updated += 1
        # Flip the post itself. Preserve prev_status only if not
        # already in report (so a re-report doesn't lose the
        # original prev_status).
        old_status = post_row["status"]
        if old_status != "report":
            self.conn.execute(
                """UPDATE posts
                      SET status = 'report',
                          prev_status = ?,
                          report_month = ?,
                          report_added_at = datetime('now')
                    WHERE id = ?""",
                (old_status, report_month, post_id)
            )
        else:
            # Idempotent refresh: keep prev_status, just update month.
            self.conn.execute(
                """UPDATE posts
                      SET report_month = ?,
                          report_added_at = COALESCE(report_added_at, datetime('now'))
                    WHERE id = ?""",
                (report_month, post_id)
            )
        self.conn.commit()
        return {
            "comments_updated": comments_updated,
            "brand_required": brand_required,
            "post_flipped": old_status != "report",
        }

    def undo_post_report(self, post_id, *, actor_email=None):
        """Revert a 'report' post + all of its reported comments for
        the SAME month back to their previous status. Idempotent: a
        post not in 'report' state is left alone.
        Returns: {comments_reverted, post_restored_to}.
        """
        post_row = self.conn.execute(
            "SELECT id, status, prev_status, report_month FROM posts WHERE id = ?",
            (post_id,)
        ).fetchone()
        if not post_row or post_row["status"] != "report":
            return {"comments_reverted": 0, "post_restored_to": None}
        month = post_row["report_month"]
        # Revert each report-month comment under this post.
        rows = self.conn.execute(
            """SELECT id FROM comments
                WHERE post_id = ?
                  AND status = 'report'
                  AND COALESCE(report_month, '') = COALESCE(?, '')""",
            (post_id, month)
        ).fetchall()
        comments_reverted = 0
        for r in rows:
            if self.undo_report(r["id"], "comment",
                                 actor_email=actor_email) is not None:
                comments_reverted += 1
        # Restore the post.
        restore_to = post_row["prev_status"] or "complete"
        self.conn.execute(
            """UPDATE posts
                  SET status = ?, prev_status = NULL,
                      report_month = NULL, report_added_at = NULL
                WHERE id = ?""",
            (restore_to, post_id)
        )
        self.conn.commit()
        return {"comments_reverted": comments_reverted,
                "post_restored_to": restore_to}

    def bulk_move_posts_to_report(self, *, post_ids, report_month,
                                    actor_email=None,
                                    brand_id_override=None):
        """Iterate `post_ids` and call `move_post_to_report` for each.
        Aggregates counts. Used by the Mark Reported All flow.
        """
        if not post_ids or not report_month:
            return {"posts_updated": 0, "comments_updated": 0,
                    "brand_required": 0, "skipped": 0}
        posts_updated = 0
        comments_updated = 0
        brand_required = 0
        skipped = 0
        for pid in post_ids:
            try:
                pid_int = int(pid)
            except (TypeError, ValueError):
                skipped += 1; continue
            res = self.move_post_to_report(
                pid_int, report_month=report_month,
                actor_email=actor_email,
                brand_id_override=brand_id_override,
            )
            if res.get("post_flipped") or res.get("comments_updated"):
                posts_updated += 1
            else:
                skipped += 1
            comments_updated += res.get("comments_updated", 0)
            brand_required += res.get("brand_required", 0)
        return {"posts_updated": posts_updated,
                "comments_updated": comments_updated,
                "brand_required": brand_required,
                "skipped": skipped}

    def get_report_months_for_client(self, client_id):
        """Distinct report_month values for a client's deliverables,
        split per pipeline. Used by the portal dashboard's By Month
        view.

        Each row carries Mentions (search_comments) and HQ Mentions
        (Live Subs posts) counters separately. HQ Mentions are
        counted at POST level (1 post = 1 HQ Mention regardless of
        how many of its comments are reported); Mentions are counted
        at comment level (1 search_comment = 1 Mention).

        Returns: [{
            month: 'YYYY-MM',
            mentions_total, mentions_live, mentions_removed,
            hq_total, hq_live, hq_removed,
            # back-compat aggregates (sum of both pipelines):
            total, live, removed,
        }, ...]

        `live` vs `removed` for HQ Mentions follows the derived rule
        we use elsewhere: a post is removed iff `posts.status =
        'removed'` OR any of its reported comments has
        `status = 'removed'`. Otherwise live.
        """
        brand_ids = self.client_brand_ids(client_id)
        if not brand_ids:
            return []
        ph = ",".join("?" * len(brand_ids))
        match_c = (
            f"("
            f"  c.brand_id IN ({ph})"
            f"  OR (c.brand_id IS NULL AND p.brand_id IN ({ph}))"
            f"  OR (c.brand_id IS NULL AND p.id IN (SELECT post_id FROM post_brands WHERE brand_id IN ({ph})))"
            f")"
        )
        match_sc = (
            f"("
            f"  sc.brand_id IN ({ph})"
            f"  OR (sc.brand_id IS NULL AND sp.brand_id IN ({ph}))"
            f")"
        )
        params_c = brand_ids * 3
        params_sc = brand_ids * 2
        # q_hq: one row per (month, post_id). is_removed = 1 iff
        # ANY of:
        #   - post.status = 'removed'
        #   - any reported comment is status='removed'
        #   - any reported comment is status='report' but has no
        #     Reddit URL (= never actually posted)
        #   - the post has no deployed HQ root (no Mention Link
        #     anchor on Reddit)
        # Mirrors derived_status used by the month-page chip — keep
        # these two in sync.
        # is_removed mirrors `get_posts_for_client_month`'s per-row
        # helper — keep these two in sync. A post is "live" iff it
        # has at least ONE HQ root (comment_type='hq' AND
        # parent_comment_id IS NULL) with a Reddit URL whose status
        # is not 'removed' / 'replace'. Otherwise it's removed.
        # Non-HQ-root reported comments don't gate the verdict.
        q_hq = f"""
            SELECT c.report_month AS month,
                   c.post_id AS post_id,
                   MAX(CASE WHEN p.status = 'removed' THEN 1
                            WHEN NOT EXISTS (
                                SELECT 1 FROM comments hq
                                 WHERE hq.post_id = c.post_id
                                   AND hq.comment_type = 'hq'
                                   AND hq.parent_comment_id IS NULL
                                   AND hq.status NOT IN ('removed', 'replace')
                                   AND TRIM(COALESCE(hq.reddit_comment_url, '')) != ''
                            ) THEN 1
                            ELSE 0 END) AS is_removed,
                   -- `is_replace` is a sub-count of `is_removed`:
                   -- post is removed AND the verdict is driven by a
                   -- 'replace' HQ root (rather than a hard removal or
                   -- missing HQ). Surfaced as a separate chip on the
                   -- dashboard cards so the admin can see how many of
                   -- the "removed" deliverables are still eligible for
                   -- redeploy.
                   MAX(CASE
                       WHEN p.status = 'removed' THEN 0
                       WHEN EXISTS (
                           SELECT 1 FROM comments hq
                            WHERE hq.post_id = c.post_id
                              AND hq.comment_type = 'hq'
                              AND hq.parent_comment_id IS NULL
                              AND hq.status NOT IN ('removed', 'replace')
                              AND TRIM(COALESCE(hq.reddit_comment_url, '')) != ''
                       ) THEN 0
                       WHEN EXISTS (
                           SELECT 1 FROM comments hq
                            WHERE hq.post_id = c.post_id
                              AND hq.comment_type = 'hq'
                              AND hq.parent_comment_id IS NULL
                              AND hq.status = 'replace'
                       ) THEN 1
                       ELSE 0
                   END) AS is_replace
              FROM comments c
              JOIN posts p ON c.post_id = p.id
             WHERE {match_c}
               AND c.report_month IS NOT NULL
               AND c.status IN ('report', 'removed', 'replace')
             GROUP BY c.report_month, c.post_id
        """
        # Counts are mutually exclusive — a single row is either
        # live, removed, or replace (never two at once). This was a
        # confusing UX before when `removed` included replace and
        # the dashboard showed "12 removed · 4 replace" looking like
        # 16 not-live rows when it was really 12 (4 of which are
        # eligible for redeploy).
        q_mentions = f"""
            SELECT sc.report_month AS month,
                   COUNT(*) AS total,
                   SUM(CASE WHEN sc.status = 'report' THEN 1 ELSE 0 END) AS live,
                   SUM(CASE WHEN sc.status = 'removed' THEN 1 ELSE 0 END) AS removed,
                   SUM(CASE WHEN sc.status = 'replace' THEN 1 ELSE 0 END) AS replace_cnt
              FROM search_comments sc
              JOIN search_posts sp ON sc.search_post_id = sp.id
             WHERE {match_sc}
               AND sc.report_month IS NOT NULL
               AND sc.status IN ('report', 'removed', 'replace')
             GROUP BY sc.report_month
        """
        agg = {}
        # Per-post seen set keyed by (month, post_id) so the post-
        # only query below doesn't double-count a post that already
        # showed up via its reported comments.
        seen_posts = set()
        for r in self.conn.execute(q_hq, params_c).fetchall():
            m = r["month"]
            if not m:
                continue
            bucket = agg.setdefault(m, {
                "month": m,
                "mentions_total": 0, "mentions_live": 0, "mentions_removed": 0,
                "mentions_replace": 0,
                "hq_total": 0, "hq_live": 0, "hq_removed": 0,
                "hq_replace": 0,
            })
            bucket["hq_total"] += 1
            seen_posts.add((m, r["post_id"]))
            # Mutually exclusive buckets: a post is in EXACTLY ONE of
            # live / removed / replace. is_removed=1 + is_replace=1
            # means the verdict is 'replace'; is_removed=1 alone
            # means strict 'removed'.
            if r["is_replace"]:
                bucket["hq_replace"] += 1
            elif r["is_removed"]:
                bucket["hq_removed"] += 1
            else:
                bucket["hq_live"] += 1
        # Post-only follow-up: posts.status='report' that the main
        # q_hq query missed because NO comment under the post is in
        # ('report','removed','replace'). Two real cases land here:
        #
        #   (a) The new "1 post = 1 HQ Mention" flow flipped the
        #       parent post to 'report' but the HQ root is still in
        #       'deployed' / 'paid' (move_post_to_report doesn't
        #       always touch the HQ comment). The HQ comment IS live
        #       on Reddit with a URL — so the per-row helper marks
        #       the post 'live', but the aggregate used to mark it
        #       'removed' because it never inspected the HQ root.
        #       That's the bug behind "8 live / 3 removed in tile
        #       vs 9 live / 2 removed in detail".
        #
        #   (b) No HQ root exists at all (post was reported without
        #       any HQ thread deployed). Correctly 'removed' because
        #       there's no Mention Link to anchor to.
        #
        # Apply the same live / replace / removed classification used
        # by q_hq above so the two queries agree.
        q_hq_post_only = f"""
            SELECT p.report_month AS month,
                   p.id AS post_id,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM comments hq
                        WHERE hq.post_id = p.id
                          AND hq.comment_type = 'hq'
                          AND hq.parent_comment_id IS NULL
                          AND hq.status NOT IN ('removed', 'replace')
                          AND TRIM(COALESCE(hq.reddit_comment_url, '')) != ''
                   ) THEN 0 ELSE 1 END AS is_removed,
                   CASE
                       WHEN EXISTS (
                           SELECT 1 FROM comments hq
                            WHERE hq.post_id = p.id
                              AND hq.comment_type = 'hq'
                              AND hq.parent_comment_id IS NULL
                              AND hq.status NOT IN ('removed', 'replace')
                              AND TRIM(COALESCE(hq.reddit_comment_url, '')) != ''
                       ) THEN 0
                       WHEN EXISTS (
                           SELECT 1 FROM comments hq
                            WHERE hq.post_id = p.id
                              AND hq.comment_type = 'hq'
                              AND hq.parent_comment_id IS NULL
                              AND hq.status = 'replace'
                       ) THEN 1
                       ELSE 0
                   END AS is_replace
              FROM posts p
             WHERE p.status = 'report'
               AND p.report_month IS NOT NULL
               AND (
                 p.brand_id IN ({ph})
                 OR p.id IN (SELECT post_id FROM post_brands WHERE brand_id IN ({ph}))
               )
        """
        for r in self.conn.execute(q_hq_post_only, brand_ids * 2).fetchall():
            m = r["month"]
            if not m or (m, r["post_id"]) in seen_posts:
                continue
            bucket = agg.setdefault(m, {
                "month": m,
                "mentions_total": 0, "mentions_live": 0, "mentions_removed": 0,
                "mentions_replace": 0,
                "hq_total": 0, "hq_live": 0, "hq_removed": 0,
                "hq_replace": 0,
            })
            bucket["hq_total"] += 1
            # Mutually exclusive buckets — same priority as q_hq.
            if r["is_replace"]:
                bucket["hq_replace"] += 1
            elif r["is_removed"]:
                bucket["hq_removed"] += 1
            else:
                bucket["hq_live"] += 1
            seen_posts.add((m, r["post_id"]))
        for r in self.conn.execute(q_mentions, params_sc).fetchall():
            m = r["month"]
            if not m:
                continue
            bucket = agg.setdefault(m, {
                "month": m,
                "mentions_total": 0, "mentions_live": 0, "mentions_removed": 0,
                "mentions_replace": 0,
                "hq_total": 0, "hq_live": 0, "hq_removed": 0,
                "hq_replace": 0,
            })
            bucket["mentions_total"] += r["total"] or 0
            bucket["mentions_live"] += r["live"] or 0
            bucket["mentions_removed"] += r["removed"] or 0
            bucket["mentions_replace"] += r["replace_cnt"] or 0
        # 'replaced' (replacement PROVIDED) — a delivered deliverable, distinct from the
        # pending 'replace' bucket above. Counted via a focused query and merged per month,
        # SPLIT by pipeline (mentions = search_comments, hq = comments) so each pipeline's
        # card can show its own replaced chip — mirrors the By-Brand aggregate.
        replaced_split = self.get_replaced_split_for_client(client_id)
        for m, split in replaced_split.items():
            bucket = agg.setdefault(m, {
                "month": m,
                "mentions_total": 0, "mentions_live": 0, "mentions_removed": 0,
                "mentions_replace": 0, "hq_total": 0, "hq_live": 0, "hq_removed": 0,
                "hq_replace": 0,
            })
            bucket["mentions_replaced"] = split.get("mentions", 0)
            bucket["hq_replaced"] = split.get("hq", 0)
        # Back-compat aggregates so callers that read .total / .live
        # / .removed (older callsites + the brand overview page) keep
        # working without churn.
        for b in agg.values():
            b.setdefault("mentions_replaced", 0)
            b.setdefault("hq_replaced", 0)
            # 'replaced' = a delivered replacement that restores an ALREADY-counted
            # deliverable (the removed original is already in total). So it counts toward
            # LIVE (the deliverable is live again) but NOT toward TOTAL (avoid double-count).
            b["replaced"] = b["mentions_replaced"] + b["hq_replaced"]
            b["total"] = b["mentions_total"] + b["hq_total"]
            b["live"] = b["mentions_live"] + b["hq_live"] + b["replaced"]
            b["removed"] = b["mentions_removed"] + b["hq_removed"]
            b["replace"] = b["mentions_replace"] + b["hq_replace"]
        return sorted(agg.values(), key=lambda x: x["month"], reverse=True)

    def get_replaced_split_for_client(self, client_id):
        """Per-month count of 'replaced' deliverables for this client's brands, SPLIT by
        pipeline: hq = Live Subs `comments`, mentions = `search_comments`. Returns
        {month: {"mentions": n, "hq": n}}. 'replaced' = a delivered replacement for a
        removed-but-recent comment; distinct from the pending 'replace' bucket."""
        brand_ids = self.client_brand_ids(client_id)
        if not brand_ids:
            return {}
        ph = ",".join("?" * len(brand_ids))
        out = {}
        def _cell(m):
            return out.setdefault(m, {"mentions": 0, "hq": 0})
        # hq = comments
        q_hq = f"""SELECT c.report_month AS m, COUNT(*) AS n
                     FROM comments c LEFT JOIN posts p ON c.post_id = p.id
                    WHERE c.status = 'replaced' AND c.report_month IS NOT NULL
                      AND ( c.brand_id IN ({ph})
                            OR (c.brand_id IS NULL AND p.brand_id IN ({ph}))
                            OR (c.brand_id IS NULL AND p.id IN (SELECT post_id FROM post_brands WHERE brand_id IN ({ph}))) )
                    GROUP BY c.report_month"""
        for r in self.conn.execute(q_hq, brand_ids * 3).fetchall():
            if r["m"]:
                _cell(r["m"])["hq"] += (r["n"] or 0)
        # mentions = search_comments
        q_men = f"""SELECT sc.report_month AS m, COUNT(*) AS n
                      FROM search_comments sc LEFT JOIN search_posts sp ON sc.search_post_id = sp.id
                     WHERE sc.status = 'replaced' AND sc.report_month IS NOT NULL
                       AND ( sc.brand_id IN ({ph})
                             OR (sc.brand_id IS NULL AND sp.brand_id IN ({ph})) )
                     GROUP BY sc.report_month"""
        for r in self.conn.execute(q_men, brand_ids * 2).fetchall():
            if r["m"]:
                _cell(r["m"])["mentions"] += (r["n"] or 0)
        return out

    def get_replaced_counts_for_client(self, client_id):
        """Per-month TOTAL count of 'replaced' deliverables (both pipelines) for this
        client's brands. Returns {month: count}. Kept as a thin wrapper over the split."""
        return {m: (s.get("mentions", 0) + s.get("hq", 0))
                for m, s in self.get_replaced_split_for_client(client_id).items()}

    def update_live_stats(self, comment_id, source, upvotes, num_replies):
        """Persist a single Reddit engagement snapshot. Called from
        the /api/comments/live-stats task per successful fetch.

        `upvotes` and `num_replies` are stored as-is (NULL → cleared).
        `last_stats_at` is stamped to UTC now so the UI can show
        freshness without us needing a separate query.
        """
        table = "comments" if source == "comment" else "search_comments"
        try:
            self.conn.execute(
                f"""UPDATE {table}
                       SET upvotes = ?, num_replies = ?,
                           last_stats_at = datetime('now')
                     WHERE id = ?""",
                (upvotes, num_replies, comment_id)
            )
            self.conn.commit()
        except Exception as e:
            # Best-effort: live-stats is a refresh, not a write
            # path. Log and continue so one bad row doesn't kill
            # the whole batch.
            print(f"[live-stats] persist failed cid={comment_id} src={source}: {e}", flush=True)

    def get_report_aggregate_for_client(self, client_id):
        """Flat per-(brand, month) aggregate, split per pipeline.

        Drives the dashboard's By Brand cards — JS slices these rows
        by brand + month + status to keep the cards interactive
        without re-hitting the server.

        Returns: [{
            brand_id, brand_name, month,
            mentions_total, mentions_live, mentions_removed,
            hq_total, hq_live, hq_removed,
            total, live, removed,    # back-compat aggregates
        }, ...]

        Brand resolution chain matches
        `get_comments_for_client_month`. HQ Mentions are counted at
        POST level (1 post = 1 HQ Mention regardless of how many of
        its comments are reported).
        """
        brand_ids = self.client_brand_ids(client_id)
        if not brand_ids:
            return []
        ph = ",".join("?" * len(brand_ids))
        # HQ Mentions: one row per (resolved_brand, month, post_id)
        # with an is_removed flag derived from post.status + comment
        # status + URL presence + HQ-root existence (matches the
        # derived_status rule used by the month-page chip — keep
        # these two in sync).
        q_hq = f"""
            SELECT
              CASE
                WHEN c.brand_id IS NOT NULL THEN c.brand_id
                WHEN p.brand_id IS NOT NULL THEN p.brand_id
                ELSE (SELECT pb.brand_id FROM post_brands pb WHERE pb.post_id = p.id LIMIT 1)
              END AS resolved_brand_id,
              c.report_month AS month,
              c.post_id AS post_id,
              MAX(CASE WHEN p.status = 'removed' THEN 1
                       WHEN NOT EXISTS (
                           SELECT 1 FROM comments hq
                            WHERE hq.post_id = c.post_id
                              AND hq.comment_type = 'hq'
                              AND hq.parent_comment_id IS NULL
                              AND hq.status NOT IN ('removed', 'replace')
                              AND TRIM(COALESCE(hq.reddit_comment_url, '')) != ''
                       ) THEN 1
                       ELSE 0 END) AS is_removed,
              -- is_replace is a sub-count of is_removed (same logic as
              -- the months aggregate). Surfaced separately so the
              -- brand-card UI can show a Replace chip alongside Live /
              -- Removed without changing the totals.
              MAX(CASE
                  WHEN p.status = 'removed' THEN 0
                  WHEN EXISTS (
                      SELECT 1 FROM comments hq
                       WHERE hq.post_id = c.post_id
                         AND hq.comment_type = 'hq'
                         AND hq.parent_comment_id IS NULL
                         AND hq.status NOT IN ('removed', 'replace')
                         AND TRIM(COALESCE(hq.reddit_comment_url, '')) != ''
                  ) THEN 0
                  WHEN EXISTS (
                      SELECT 1 FROM comments hq
                       WHERE hq.post_id = c.post_id
                         AND hq.comment_type = 'hq'
                         AND hq.parent_comment_id IS NULL
                         AND hq.status = 'replace'
                  ) THEN 1
                  ELSE 0
              END) AS is_replace
            FROM comments c
            JOIN posts p ON c.post_id = p.id
            WHERE c.report_month IS NOT NULL
              AND c.status IN ('report', 'removed', 'replace')
              AND (
                c.brand_id IN ({ph})
                OR (c.brand_id IS NULL AND p.brand_id IN ({ph}))
                OR (c.brand_id IS NULL AND p.id IN (SELECT post_id FROM post_brands WHERE brand_id IN ({ph})))
              )
            GROUP BY resolved_brand_id, c.report_month, c.post_id
        """
        q_mentions = f"""
            SELECT
              COALESCE(sc.brand_id, sp.brand_id) AS resolved_brand_id,
              sc.report_month AS month,
              sc.status AS status,
              COUNT(*) AS cnt
            FROM search_comments sc
            JOIN search_posts sp ON sc.search_post_id = sp.id
            WHERE sc.report_month IS NOT NULL
              AND sc.status IN ('report', 'removed', 'replace')
              AND (
                sc.brand_id IN ({ph})
                OR (sc.brand_id IS NULL AND sp.brand_id IN ({ph}))
              )
            GROUP BY resolved_brand_id, sc.report_month, sc.status
        """
        cells = {}
        # Per-(brand, month, post_id) seen set so the post-only
        # follow-up query below doesn't double-count.
        seen_posts = set()
        def _cell(bid, month):
            key = (bid, month)
            return cells.setdefault(key, {
                "mentions_total": 0, "mentions_live": 0, "mentions_removed": 0,
                "mentions_replace": 0, "mentions_replaced": 0,
                "hq_total": 0, "hq_live": 0, "hq_removed": 0,
                "hq_replace": 0, "hq_replaced": 0,
            })
        for r in self.conn.execute(q_hq, brand_ids * 3).fetchall():
            slot = _cell(r["resolved_brand_id"], r["month"])
            slot["hq_total"] += 1
            seen_posts.add((r["resolved_brand_id"], r["month"], r["post_id"]))
            # Mutually exclusive buckets — same rule as the months
            # aggregate.
            if r["is_replace"]:
                slot["hq_replace"] += 1
            elif r["is_removed"]:
                slot["hq_removed"] += 1
            else:
                slot["hq_live"] += 1
        # Post-only follow-up — same scope as the months-aggregate
        # version above. Apply the live / replace / removed
        # classification by inspecting the HQ root directly so we
        # don't over-count 'removed' for posts whose HQ comment is
        # still 'deployed'/'paid' but live on Reddit (legacy flow
        # where move_post_to_report didn't flip the HQ status).
        q_hq_post_only = f"""
            SELECT
              CASE
                WHEN p.brand_id IS NOT NULL THEN p.brand_id
                ELSE (SELECT pb.brand_id FROM post_brands pb WHERE pb.post_id = p.id LIMIT 1)
              END AS resolved_brand_id,
              p.report_month AS month,
              p.id AS post_id,
              CASE WHEN EXISTS (
                  SELECT 1 FROM comments hq
                   WHERE hq.post_id = p.id
                     AND hq.comment_type = 'hq'
                     AND hq.parent_comment_id IS NULL
                     AND hq.status NOT IN ('removed', 'replace')
                     AND TRIM(COALESCE(hq.reddit_comment_url, '')) != ''
              ) THEN 0 ELSE 1 END AS is_removed,
              CASE
                  WHEN EXISTS (
                      SELECT 1 FROM comments hq
                       WHERE hq.post_id = p.id
                         AND hq.comment_type = 'hq'
                         AND hq.parent_comment_id IS NULL
                         AND hq.status NOT IN ('removed', 'replace')
                         AND TRIM(COALESCE(hq.reddit_comment_url, '')) != ''
                  ) THEN 0
                  WHEN EXISTS (
                      SELECT 1 FROM comments hq
                       WHERE hq.post_id = p.id
                         AND hq.comment_type = 'hq'
                         AND hq.parent_comment_id IS NULL
                         AND hq.status = 'replace'
                  ) THEN 1
                  ELSE 0
              END AS is_replace
            FROM posts p
            WHERE p.status = 'report'
              AND p.report_month IS NOT NULL
              AND (
                p.brand_id IN ({ph})
                OR p.id IN (SELECT post_id FROM post_brands WHERE brand_id IN ({ph}))
              )
        """
        for r in self.conn.execute(q_hq_post_only, brand_ids * 2).fetchall():
            key = (r["resolved_brand_id"], r["month"], r["post_id"])
            if key in seen_posts:
                continue
            slot = _cell(r["resolved_brand_id"], r["month"])
            slot["hq_total"] += 1
            if r["is_replace"]:
                slot["hq_replace"] += 1
            elif r["is_removed"]:
                slot["hq_removed"] += 1
            else:
                slot["hq_live"] += 1
            seen_posts.add(key)
        for r in self.conn.execute(q_mentions, brand_ids * 2).fetchall():
            slot = _cell(r["resolved_brand_id"], r["month"])
            slot["mentions_total"] += r["cnt"]
            # Mutually exclusive counts — no double-counting across
            # the three buckets.
            if r["status"] == "report":
                slot["mentions_live"] += r["cnt"]
            elif r["status"] == "removed":
                slot["mentions_removed"] += r["cnt"]
            elif r["status"] == "replace":
                slot["mentions_replace"] += r["cnt"]
        # 'replaced' (delivered replacement) per (brand, month), split by pipeline. These
        # rows are NOT in the report/removed/replace buckets above, so rolling them into
        # live/total below never double-counts. Mirrors the months-view rollup so the two
        # dashboard tabs agree.
        q_replaced_sc = f"""
            SELECT COALESCE(sc.brand_id, sp.brand_id) AS resolved_brand_id,
                   sc.report_month AS month, COUNT(*) AS cnt
              FROM search_comments sc JOIN search_posts sp ON sc.search_post_id = sp.id
             WHERE sc.status = 'replaced' AND sc.report_month IS NOT NULL
               AND (sc.brand_id IN ({ph}) OR (sc.brand_id IS NULL AND sp.brand_id IN ({ph})))
             GROUP BY resolved_brand_id, sc.report_month
        """
        for r in self.conn.execute(q_replaced_sc, brand_ids * 2).fetchall():
            _cell(r["resolved_brand_id"], r["month"])["mentions_replaced"] += r["cnt"]
        q_replaced_c = f"""
            SELECT CASE
                     WHEN c.brand_id IS NOT NULL THEN c.brand_id
                     WHEN p.brand_id IS NOT NULL THEN p.brand_id
                     ELSE (SELECT pb.brand_id FROM post_brands pb WHERE pb.post_id = p.id LIMIT 1)
                   END AS resolved_brand_id,
                   c.report_month AS month, COUNT(*) AS cnt
              FROM comments c JOIN posts p ON c.post_id = p.id
             WHERE c.status = 'replaced' AND c.report_month IS NOT NULL
               AND ( c.brand_id IN ({ph})
                     OR (c.brand_id IS NULL AND p.brand_id IN ({ph}))
                     OR (c.brand_id IS NULL AND p.id IN (SELECT post_id FROM post_brands WHERE brand_id IN ({ph}))) )
             GROUP BY resolved_brand_id, c.report_month
        """
        for r in self.conn.execute(q_replaced_c, brand_ids * 3).fetchall():
            _cell(r["resolved_brand_id"], r["month"])["hq_replaced"] += r["cnt"]
        # Resolve brand names in one shot (one IN-clause per call).
        present_brand_ids = [bid for (bid, _m) in cells.keys() if bid is not None]
        names = {}
        if present_brand_ids:
            ph2 = ",".join("?" * len(present_brand_ids))
            for r in self.conn.execute(
                f"SELECT id, name FROM brands WHERE id IN ({ph2})",
                present_brand_ids
            ).fetchall():
                names[r["id"]] = r["name"]
        out = []
        for (bid, month), slot in cells.items():
            row = {
                "brand_id": bid,
                "brand_name": names.get(bid) or "(unbranded)",
                "month": month,
                "mentions_total": slot["mentions_total"],
                "mentions_live": slot["mentions_live"],
                "mentions_removed": slot["mentions_removed"],
                "mentions_replace": slot["mentions_replace"],
                "mentions_replaced": slot["mentions_replaced"],
                "hq_total": slot["hq_total"],
                "hq_live": slot["hq_live"],
                "hq_removed": slot["hq_removed"],
                "hq_replace": slot["hq_replace"],
                "hq_replaced": slot["hq_replaced"],
            }
            # Back-compat aggregates so JS that still reads .total /
            # .live / .removed (or any older caller) keeps working.
            # 'replaced' (delivered) rolls into live + total (it's a live mention; not in
            # the report/removed/replace buckets, so no double-count) — consistent with
            # the months-view rollup.
            _replaced = row["mentions_replaced"] + row["hq_replaced"]
            row["replaced"] = _replaced
            # replaced restores an already-counted deliverable → into LIVE, NOT total.
            row["total"] = row["mentions_total"] + row["hq_total"]
            row["live"] = row["mentions_live"] + row["hq_live"] + _replaced
            row["removed"] = row["mentions_removed"] + row["hq_removed"]
            row["replace"] = row["mentions_replace"] + row["hq_replace"]
            out.append(row)
        # Stable sort: brand name asc, month desc.
        out.sort(key=lambda r: (r["brand_name"].lower(), -1 * int((r["month"] or "0000-00").replace("-", ""))))
        return out

    def get_posts_for_client_month(self, client_id, month):
        """Post-grouped view of a client's reported deliverables for
        a month. Returns:

            {
                "posts": [
                    { id, title, subreddit_name, brand_name, posted_at,
                      deployed_at, reddit_url, comments: [comment_dicts] },
                    ...
                ],
                "search_comments": [comment_dicts],  # flat — no post layer
            }

        Posts are sorted newest-first by deployed/posted date.
        Comments within a post are sorted by `posted_at` ascending so
        the thread reads top-down. Visibility is brand-based,
        identical to `get_comments_for_client_month`.
        """
        flat = self.get_comments_for_client_month(client_id, month)
        # Live Search comments stay flat — they have no Live Subs post.
        search_comments = [r for r in flat if r.get("source") == "search_comment"]
        live_comments = [r for r in flat if r.get("source") == "comment"]
        # Map every reported Live Subs comment to its parent post id.
        cmt_ids = [int(c["id"]) for c in live_comments if c.get("id") is not None]
        cid_to_pid = {}
        if cmt_ids:
            ph = ",".join("?" * len(cmt_ids))
            for r in self.conn.execute(
                f"SELECT id, post_id FROM comments WHERE id IN ({ph})",
                cmt_ids
            ).fetchall():
                cid_to_pid[r["id"]] = r["post_id"]

        # Also include posts in status='report' for this month that
        # have NO reported comments — the new single-action report
        # flow flips just the post (no comments) when no HQ root is
        # deployed. Those should still appear on the client report
        # so the admin sees the deliverable, marked Removed.
        brand_ids = self.client_brand_ids(client_id)
        post_only_ids = []
        if brand_ids:
            bph = ",".join("?" * len(brand_ids))
            for r in self.conn.execute(
                f"""SELECT p.id FROM posts p
                     WHERE p.status = 'report'
                       AND p.report_month = ?
                       AND (
                         p.brand_id IN ({bph})
                         OR p.id IN (SELECT post_id FROM post_brands WHERE brand_id IN ({bph}))
                       )""",
                [month] + brand_ids + brand_ids
            ).fetchall():
                post_only_ids.append(r["id"])

        all_post_ids = sorted(set(cid_to_pid.values()) | set(post_only_ids))
        if not all_post_ids:
            return {"posts": [], "search_comments": search_comments}
        post_ids = all_post_ids
        php = ",".join("?" * len(post_ids))
        post_rows = self.conn.execute(
            f"""SELECT p.id, p.title, p.status, p.posted_at,
                       p.deployed_at, p.paid_at, p.created_at,
                       s.name AS subreddit_name,
                       b.name AS brand_name,
                       (SELECT pu.reddit_url FROM post_urls pu WHERE pu.post_id = p.id LIMIT 1) AS reddit_url
                  FROM posts p
             LEFT JOIN subreddits s ON p.subreddit_id = s.id
             LEFT JOIN brands b ON p.brand_id = b.id
                 WHERE p.id IN ({php})""",
            post_ids
        ).fetchall()
        posts = {r["id"]: dict(r) for r in post_rows}
        # Mention Link: the URL of the HQ root comment under each post
        # — the cluster head where the brand mention lives. The
        # client report uses this as the deliverable's primary
        # engagement link. If a post has multiple HQ threads, take
        # the earliest-posted root that has a URL.
        #
        # We also gather the FULL list of HQ roots per post so the
        # derived_status helper below can decide Live vs Removed
        # based on whether ANY HQ root is currently live on Reddit:
        # one removed HQ + one redeployed HQ should still read as
        # Live (the brand IS live, the removed row is just history).
        # Non-HQ-root reported comments (legacy replies / siblings)
        # never gate the verdict.
        mention_rows = self.conn.execute(
            f"""SELECT post_id, id AS hq_id, reddit_comment_url, status
                  FROM comments
                 WHERE post_id IN ({php})
                   AND comment_type = 'hq'
                   AND parent_comment_id IS NULL
              ORDER BY post_id, COALESCE(posted_at, deployed_at, created_at)""",
            post_ids
        ).fetchall()
        mention_by_post = {}        # pid -> URL (earliest HQ with a URL)
        hq_roots_by_post = {}       # pid -> list of {"id","status","url"}
        for r in mention_rows:
            hq_roots_by_post.setdefault(r["post_id"], []).append({
                "id": r["hq_id"],
                "status": (r["status"] or "").lower(),
                "url": (r["reddit_comment_url"] or "").strip(),
            })
            if (r["reddit_comment_url"] and r["post_id"] not in mention_by_post):
                mention_by_post[r["post_id"]] = r["reddit_comment_url"]
        for pid, post in posts.items():
            post["mention_link"] = mention_by_post.get(pid)
            post["_hq_roots"] = hq_roots_by_post.get(pid) or []
        # Attach comments to their posts.
        for post in posts.values():
            post["comments"] = []
        for c in live_comments:
            pid = cid_to_pid.get(c["id"])
            if pid in posts:
                posts[pid]["comments"].append(c)
        # Within each post, sort comments by posted_at asc (oldest
        # first — thread reads top-down).
        for post in posts.values():
            post["comments"].sort(
                key=lambda r: (r.get("posted_at") or r.get("deployed_at") or "")
            )
        # Derived HQ Mentions status for each post.
        #
        # Verdict is driven by the post's HQ ROOT comments (rows with
        # comment_type='hq' AND parent_comment_id IS NULL). Other
        # reported comments under the post (replies, siblings from
        # the legacy multi-comment-report flow) do NOT gate the
        # verdict.
        #
        # Across the HQ root rows we pick the BEST classification:
        #
        #   live    : at least one HQ root has a Reddit URL AND its
        #             status is not 'removed'/'replace'. (status of
        #             'report' / 'deployed' / 'paid' all count as
        #             live as long as the URL is set — the comment
        #             IS on Reddit.)
        #   replace : no live HQ root, but at least one HQ root has
        #             status='replace' (recent removal — eligible
        #             for redeploy). Chip is amber; rolls up under
        #             Removed for KPI counting.
        #   removed : no live HQ root AND no replace HQ root. Either
        #             no HQ root at all, or every HQ root is removed
        #             / has no URL.
        #
        # `post.status == 'removed'` always wins regardless of HQ
        # state — the post itself was reported as removed.
        for post in posts.values():
            post_status = (post.get("status") or "").lower()
            cmts = post["comments"]
            hq_roots = post.get("_hq_roots") or []

            # Classify each HQ root for the reason text + verdict
            # selection.
            # A truly-live HQ root = on Reddit with a working status. A 'replaced' root is
            # ALSO live (the deliverable was restored via a replacement) but we surface it
            # as its OWN verdict so the client report shows replacements distinctly rather
            # than folding them silently into 'live'.
            truly_live_hq = next(
                (h for h in hq_roots
                 if h["url"] and h["status"] in ("report", "deployed", "paid")),
                None,
            )
            replaced_hq = next(
                (h for h in hq_roots if h["url"] and h["status"] == "replaced"),
                None,
            )
            replace_hq = next(
                (h for h in hq_roots if h["status"] == "replace"),
                None,
            )

            reason = None
            verdict = None  # 'replaced'/'replace' set here; 'live'/'removed' resolved below
            if post_status == "removed":
                reason = (f"post.status='removed' "
                          f"(prev_status={post.get('prev_status')!r})")
            elif truly_live_hq is not None:
                # Healthy path — no verdict reason; falls through
                # to the "all clear" branch below.
                pass
            elif replaced_hq is not None:
                reason = (f"HQ root comment #{replaced_hq['id']} "
                          f"status='replaced' (a replacement was delivered — the "
                          f"deliverable is live again); no other HQ root is in a "
                          f"plain-live state")
                verdict = "replaced"
            elif replace_hq is not None:
                reason = (f"HQ root comment #{replace_hq['id']} "
                          f"status='replace' (recent removal — "
                          f"eligible for redeploy); no other HQ "
                          f"root is currently live")
                verdict = "replace"
            elif not hq_roots:
                reason = ("no deployed HQ root comment under this post "
                          "(no comment_type='hq' AND parent_comment_id "
                          "IS NULL)")
            else:
                # At least one HQ root exists but none are live (and
                # none are 'replace'). Surface the worst row for
                # context.
                bad = next(
                    (h for h in hq_roots if h["status"] == "removed"),
                    None,
                ) or next(
                    (h for h in hq_roots if not h["url"]),
                    None,
                ) or hq_roots[0]
                if bad["status"] == "removed":
                    reason = (f"HQ root comment #{bad['id']} "
                              f"status='removed'; no other HQ root is "
                              f"currently live")
                elif not bad["url"]:
                    reason = (f"HQ root comment #{bad['id']} has empty "
                              f"reddit_comment_url (never actually posted)")
                else:
                    reason = (f"HQ root comment #{bad['id']} status="
                              f"{bad['status']!r} — not in a live state")

            # Render a per-comment listing in the reason so the
            # admin can see EXACTLY which comments live under this
            # post. Also list every HQ root row + its state.
            def _cmt_brief(c):
                cid = c.get("id")
                cst = (c.get("status") or "").lower()
                u = (c.get("reddit_comment_url") or "").strip()
                return f"#{cid} {cst}{' (no URL)' if not u else ''}"
            cmts_str = (", ".join(_cmt_brief(c) for c in cmts)
                        if cmts else "(none)")
            hq_brief = (
                ", ".join(
                    f"#{h['id']} status={h['status']!r} "
                    f"url={'set' if h['url'] else 'empty'}"
                    for h in hq_roots
                ) if hq_roots else "(no HQ root)"
            )
            if reason:
                post["derived_status"] = verdict or "removed"
                post["derived_status_reason"] = (
                    f"{reason} | post.status={post_status!r} | "
                    f"hq_roots: [{hq_brief}] | reported comments: "
                    f"{cmts_str}"
                )
            else:
                post["derived_status"] = "live"
                post["derived_status_reason"] = (
                    f"all clear — live HQ root #{truly_live_hq['id']} "
                    f"status={truly_live_hq['status']!r} url=set | "
                    f"post.status={post_status!r} | "
                    f"hq_roots: [{hq_brief}] | "
                    f"{len(cmts)} reported comment(s): {cmts_str}"
                )
            # Internal hint — strip before returning so JSON
            # serialization stays clean and the template doesn't
            # see it as a row attribute.
            post.pop("_hq_roots", None)
        # Posts sorted newest-first by the actual Reddit publish
        # timestamp where we have it, falling back to deployed_at /
        # paid_at / created_at for legacy rows that haven't had
        # posted_at populated yet.
        ordered = sorted(
            posts.values(),
            key=lambda p: (p.get("posted_at")
                            or p.get("deployed_at")
                            or p.get("paid_at")
                            or p.get("created_at")
                            or ""),
            reverse=True,
        )
        return {"posts": ordered, "search_comments": search_comments}

    def get_comments_for_client_month(self, client_id, month):
        """All comments for a client + month (both tables, merged + sorted
        by deployed_at desc). Brand-based visibility, same chain as
        `get_report_months_for_client`.
        """
        brand_ids = self.client_brand_ids(client_id)
        if not brand_ids:
            return []
        ph = ",".join("?" * len(brand_ids))
        match_c = (
            f"("
            f"  c.brand_id IN ({ph})"
            f"  OR (c.brand_id IS NULL AND p.brand_id IN ({ph}))"
            f"  OR (c.brand_id IS NULL AND p.id IN (SELECT post_id FROM post_brands WHERE brand_id IN ({ph})))"
            f")"
        )
        match_sc = (
            f"("
            f"  sc.brand_id IN ({ph})"
            f"  OR (sc.brand_id IS NULL AND sp.brand_id IN ({ph}))"
            f")"
        )
        params_q1 = (brand_ids * 3) + [month]
        q1 = f"""SELECT c.id, c.body, c.status, c.account_id, c.brand_id,
                        c.reddit_comment_url, c.posted_at, c.deployed_at,
                        c.report_month, c.report_added_at,
                        c.created_at, c.is_reply, c.comment_type,
                        c.upvotes, c.num_replies, c.last_stats_at,
                        'comment' AS source,
                        s.name AS subreddit_name,
                        COALESCE(b.name, pb.name) AS brand_name,
                        COALESCE(c.brand_id, p.brand_id) AS resolved_brand_id
                 FROM comments c
                 JOIN posts p ON c.post_id = p.id
                 LEFT JOIN subreddits s ON p.subreddit_id = s.id
                 LEFT JOIN brands b ON c.brand_id = b.id
                 LEFT JOIN brands pb ON p.brand_id = pb.id
                 WHERE {match_c}
                   AND c.report_month = ?
                   AND c.status IN ('report', 'removed', 'replace', 'replaced')"""
        params_q2 = (brand_ids * 2) + [month]
        q2 = f"""SELECT sc.id, sc.body, sc.status, sc.account_id, sc.brand_id,
                        sc.reddit_comment_url, sc.posted_at, sc.deployed_at,
                        sc.report_month, sc.report_added_at,
                        sc.created_at, sc.is_reply, sc.comment_type,
                        sc.upvotes, sc.num_replies, sc.last_stats_at,
                        'search_comment' AS source,
                        sp.subreddit AS subreddit_name,
                        COALESCE(b.name, spb.name) AS brand_name,
                        COALESCE(sc.brand_id, sp.brand_id) AS resolved_brand_id
                 FROM search_comments sc
                 JOIN search_posts sp ON sc.search_post_id = sp.id
                 LEFT JOIN brands b ON sc.brand_id = b.id
                 LEFT JOIN brands spb ON sp.brand_id = spb.id
                 WHERE {match_sc}
                   AND sc.report_month = ?
                   AND sc.status IN ('report', 'removed', 'replace', 'replaced')"""
        rows = []
        rows.extend(dict(r) for r in self.conn.execute(q1, params_q1).fetchall())
        rows.extend(dict(r) for r in self.conn.execute(q2, params_q2).fetchall())
        rows.sort(key=lambda r: (r.get("posted_at") or r.get("deployed_at") or ""), reverse=True)
        return rows

    def get_check_live_items_for_client_month(self, client_id, month):
        """Return every comment that should get a liveness check for
        the client's reports in this month.

        Superset of `get_comments_for_client_month`:
          - All reported comments (status IN report/removed/replace),
            from both `comments` (HQ pipeline) and `search_comments`
            (Mentions pipeline) — same set the dashboard renders.
          - Plus every HQ ROOT comment (comment_type='hq' AND
            parent_comment_id IS NULL, with a Reddit URL) attached
            to a post that's part of this month's HQ reports, EVEN
            IF the HQ root itself is still in a non-reported status
            like 'deployed' / 'paid'. The "1 post = 1 HQ Mention"
            flow flips just the parent post to 'report' and leaves
            the HQ root in its prior status — without including
            those rows here, Check Live never refreshes them, so
            their derived_status stays Live forever on the dashboard
            even when Reddit has removed the comment.

        Deduplicates by (source, id). Brand-resolution chain matches
        `get_comments_for_client_month`.
        """
        brand_ids = self.client_brand_ids(client_id)
        if not brand_ids:
            return []
        # Step 1: reported comments — identical select shape to
        # `get_comments_for_client_month` so callers can reuse
        # `_build_check_live_items` without branching.
        reported = self.get_comments_for_client_month(client_id, month)
        seen = {(r.get("source", "comment"), r["id"]) for r in reported}

        # Step 2: extra HQ roots tied to reported posts.
        ph = ",".join("?" * len(brand_ids))
        # Find all post IDs that are part of this month's HQ reports.
        # Two sources: (a) any comment with c.report_month = month
        # (via the standard brand-resolution chain), and (b) post
        # itself in status='report' with report_month = month (the
        # post-only single-action report flow).
        post_ids_rows = self.conn.execute(
            f"""SELECT DISTINCT post_id FROM (
                  SELECT c.post_id AS post_id
                    FROM comments c
                    JOIN posts p ON c.post_id = p.id
                   WHERE c.report_month = ?
                     AND (
                       c.brand_id IN ({ph})
                       OR (c.brand_id IS NULL AND p.brand_id IN ({ph}))
                       OR (c.brand_id IS NULL AND p.id IN (
                             SELECT post_id FROM post_brands
                              WHERE brand_id IN ({ph})))
                     )
                  UNION
                  SELECT p.id AS post_id
                    FROM posts p
                   WHERE p.status = 'report'
                     AND p.report_month = ?
                     AND (
                       p.brand_id IN ({ph})
                       OR p.id IN (SELECT post_id FROM post_brands
                                    WHERE brand_id IN ({ph}))
                     )
                )""",
            [month] + brand_ids * 3 + [month] + brand_ids * 2
        ).fetchall()
        post_ids = [r["post_id"] for r in post_ids_rows]
        if not post_ids:
            return reported
        pph = ",".join("?" * len(post_ids))
        # Pull every HQ root for those posts that has a Reddit URL.
        # We DON'T filter by status here — that's the whole point:
        # a 'deployed' HQ root under a reported post is exactly the
        # row that's slipping through the dashboard verdict today.
        extra_rows = self.conn.execute(
            f"""SELECT c.id, c.body, c.status, c.account_id, c.brand_id,
                       c.reddit_comment_url, c.posted_at, c.deployed_at,
                       c.report_month, c.report_added_at,
                       c.created_at, c.is_reply, c.comment_type,
                       c.upvotes, c.num_replies, c.last_stats_at,
                       'comment' AS source,
                       s.name AS subreddit_name,
                       COALESCE(b.name, pb.name) AS brand_name,
                       COALESCE(c.brand_id, p.brand_id) AS resolved_brand_id
                  FROM comments c
                  JOIN posts p ON c.post_id = p.id
             LEFT JOIN subreddits s ON p.subreddit_id = s.id
             LEFT JOIN brands b ON c.brand_id = b.id
             LEFT JOIN brands pb ON p.brand_id = pb.id
                 WHERE c.post_id IN ({pph})
                   AND c.comment_type = 'hq'
                   AND c.parent_comment_id IS NULL
                   AND TRIM(COALESCE(c.reddit_comment_url, '')) != ''""",
            post_ids
        ).fetchall()
        out = list(reported)
        for r in extra_rows:
            key = ("comment", r["id"])
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(r))
        return out

    def reconcile_replace_window(self, days=None):
        """One-shot migration: walk every 'removed' row in `comments`
        and `search_comments` whose anchor timestamp falls inside the
        14-day window and flip it to 'replace'.

        Useful when the 'replace' state was introduced after some
        comments were already auto-marked 'removed' — those rows
        otherwise stay in 'removed' forever, since the chooser only
        runs at detection time. The anchor follows the same
        posted_at → deployed_at fallback as the per-row chooser.

        Returns `{"comments": <int>, "search_comments": <int>}` with
        the number of rows promoted in each table.
        """
        if days is None:
            days = self.REPLACE_WINDOW_DAYS
        # SQLite stores the timestamps as ISO-like strings; the
        # COALESCE picks posted_at first, then deployed_at. We
        # compute the cutoff in UTC to match how those columns are
        # written everywhere else in the codebase.
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        promoted = {"comments": 0, "search_comments": 0}

        # comments
        rows = self.conn.execute(
            """SELECT id FROM comments
                WHERE status = 'removed'
                  AND COALESCE(posted_at, deployed_at) IS NOT NULL
                  AND COALESCE(posted_at, deployed_at) >= ?""",
            (cutoff,)
        ).fetchall()
        for r in rows:
            # Preserve prev_status if it's already set (from the
            # original removed flip); otherwise stash 'removed' so
            # an Unremove later still has somewhere sensible to
            # restore to.
            self.conn.execute(
                """UPDATE comments
                      SET status = 'replace',
                          prev_status = COALESCE(prev_status, 'deployed')
                    WHERE id = ? AND status = 'removed'""",
                (r["id"],)
            )
            promoted["comments"] += 1

        # search_comments
        rows = self.conn.execute(
            """SELECT id FROM search_comments
                WHERE status = 'removed'
                  AND COALESCE(posted_at, deployed_at) IS NOT NULL
                  AND COALESCE(posted_at, deployed_at) >= ?""",
            (cutoff,)
        ).fetchall()
        for r in rows:
            self.conn.execute(
                """UPDATE search_comments
                      SET status = 'replace',
                          prev_status = COALESCE(prev_status, 'deployed')
                    WHERE id = ? AND status = 'removed'""",
                (r["id"],)
            )
            promoted["search_comments"] += 1

        self.conn.commit()
        return promoted

    def get_deployed_comment_urls(self, subreddit_id):
        """Get deployed comments with their Reddit URLs for live checking."""
        rows = self.conn.execute(
            """SELECT c.id, c.reddit_comment_url
               FROM comments c
               JOIN posts p ON c.post_id = p.id
               WHERE p.subreddit_id = ? AND c.status = 'deployed' AND c.reddit_comment_url IS NOT NULL""",
            (subreddit_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_deployed_search_comment_urls(self):
        """Get search comments with Reddit URLs for live checking (any status with a URL)."""
        rows = self.conn.execute(
            """SELECT sc.id, sc.reddit_comment_url, sc.status,
                      sc.account_id, sp.subreddit, b.name as brand_name
               FROM search_comments sc
               JOIN search_posts sp ON sc.search_post_id = sp.id
               LEFT JOIN brands b ON sc.brand_id = b.id
               WHERE sc.reddit_comment_url IS NOT NULL"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_deployed_comment_urls(self):
        """Get all deployed comment URLs across both tables for live checking."""
        return self.get_filtered_comment_urls(status='deployed')

    def get_filtered_comment_urls(self, status=None, brand_id=None, subreddit_id=None,
                                   account_id=None, source_filter=None, date=None,
                                   fresh_only=False):
        """Get comment URLs matching filters for live checking.
        Returns list of dicts with id, reddit_comment_url, source, status."""
        w1, w2 = ["c.reddit_comment_url IS NOT NULL"], ["sc.reddit_comment_url IS NOT NULL"]
        p1, p2 = [], []

        if status:
            w1.append("c.status = ?"); p1.append(status)
            w2.append("sc.status = ?"); p2.append(status)
        if fresh_only:
            w1.append("c.last_live_check IS NULL")
            w2.append("sc.last_live_check IS NULL")
            # Fresh only makes sense for deployed/paid — skip drafts etc.
            if not status:
                w1.append("c.status IN ('deployed','paid')")
                w2.append("sc.status IN ('deployed','paid')")
        if brand_id:
            w1.append("c.brand_id = ?"); p1.append(brand_id)
            w2.append("sc.brand_id = ?"); p2.append(brand_id)
        if account_id:
            w1.append("c.account_id = ?"); p1.append(account_id)
            w2.append("sc.account_id = ?"); p2.append(account_id)
        if subreddit_id:
            w1.append("p.subreddit_id = ?"); p1.append(subreddit_id)
            row = self.conn.execute("SELECT name FROM subreddits WHERE id = ?", (subreddit_id,)).fetchone()
            sub_name = row["name"] if row else ""
            w2.append("LOWER(sp.subreddit) = LOWER(?)"); p2.append(sub_name)
        if date:
            w1.append("DATE(COALESCE(c.deployed_at, c.created_at)) = ?"); p1.append(date)
            w2.append("DATE(COALESCE(sc.deployed_at, sc.created_at)) = ?"); p2.append(date)

        where1 = " AND ".join(w1)
        where2 = " AND ".join(w2)

        q1 = f"""SELECT c.id, c.reddit_comment_url, 'comment' as source, c.status,
                        c.account_id, s.name as subreddit, b.name as brand_name
                 FROM comments c
                 JOIN posts p ON c.post_id = p.id
                 LEFT JOIN subreddits s ON p.subreddit_id = s.id
                 LEFT JOIN brands b ON c.brand_id = b.id
                 WHERE {where1}"""
        q2 = f"""SELECT sc.id, sc.reddit_comment_url, 'search_comment' as source, sc.status,
                        sc.account_id, sp.subreddit, b.name as brand_name
                 FROM search_comments sc
                 JOIN search_posts sp ON sc.search_post_id = sp.id
                 LEFT JOIN brands b ON sc.brand_id = b.id
                 WHERE {where2}"""

        if source_filter == 'comment':
            rows = self.conn.execute(q1, p1).fetchall()
        elif source_filter == 'search_comment':
            rows = self.conn.execute(q2, p2).fetchall()
        else:
            rows = self.conn.execute(f"{q1} UNION ALL {q2}", p1 + p2).fetchall()
        return [dict(r) for r in rows]

    def restore_comment_to_deployed(self, comment_id):
        """Restore a removed/deleted regular comment back to deployed."""
        self.conn.execute(
            "UPDATE comments SET status = 'deployed', last_live_check = datetime('now') WHERE id = ?",
            (comment_id,))
        self.conn.commit()

    def restore_search_comment_to_deployed(self, comment_id):
        """Restore a removed search comment back to deployed."""
        self.conn.execute(
            "UPDATE search_comments SET status = 'deployed', last_live_check = datetime('now') WHERE id = ?",
            (comment_id,))
        self.conn.commit()

    def get_published_posts_with_urls(self, subreddit_id):
        """Get posts that have Reddit URLs linked (published posts)."""
        rows = self.conn.execute(
            """SELECT p.*, pu.reddit_url
            FROM posts p
            JOIN post_urls pu ON pu.post_id = p.id
            WHERE p.subreddit_id = ? AND pu.reddit_url IS NOT NULL
            ORDER BY p.suggested_post_day DESC""",
            (subreddit_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_schedule_status(self, subreddit_id):
        """Get posts and comments organized by suggested_post_day."""
        posts = self.conn.execute(
            """SELECT p.*, pu.reddit_url
            FROM posts p
            LEFT JOIN post_urls pu ON pu.post_id = p.id
            WHERE p.subreddit_id = ?
            ORDER BY p.suggested_post_day, p.id""",
            (subreddit_id,)
        ).fetchall()

        schedule = {}
        for p in posts:
            p = dict(p)
            day = p["suggested_post_day"]
            if day not in schedule:
                schedule[day] = {"posts": [], "comments": []}
            schedule[day]["posts"].append(p)

        # Get comments grouped by day
        comments = self.conn.execute(
            """SELECT c.*, p.title as post_title
            FROM comments c
            JOIN posts p ON c.post_id = p.id
            WHERE p.subreddit_id = ?
            ORDER BY c.suggested_post_day, c.suggested_order""",
            (subreddit_id,)
        ).fetchall()

        for c in comments:
            c = dict(c)
            day = c["suggested_post_day"]
            if day not in schedule:
                schedule[day] = {"posts": [], "comments": []}
            schedule[day]["comments"].append(c)

        return schedule

    def get_brand_comments_with_details(self, brand_name, date_from=None, date_to=None):
        """Get all comments for a brand (by name, across all subreddits) with post+subreddit info.

        Includes both regular comments and search_comments via UNION.
        Optionally filtered by created_at date range.
        Returns list of dicts with comment fields + post_title, post_reddit_url, subreddit_name, subreddit_id.
        """
        # Get all brand IDs matching this name (brands are per-subreddit)
        brand_ids = [r[0] for r in self.conn.execute(
            "SELECT id FROM brands WHERE LOWER(name) = LOWER(?)", (brand_name,)
        ).fetchall()]
        if not brand_ids:
            return []

        placeholders = ",".join("?" * len(brand_ids))

        # Regular comments
        q1 = f"""SELECT c.id, c.body, c.status, c.account_id, c.brand_id,
                       c.is_reply, c.mentions_brand, c.created_at, c.deployed_at,
                       c.paid_at, c.reddit_comment_url, c.suggested_post_day,
                       c.is_ours, c.matched_keywords,
                       'comment' as source,
                       p.title as post_title, p.subreddit_id,
                       COALESCE(s.name, '') as subreddit_name,
                       (SELECT pu.reddit_url FROM post_urls pu WHERE pu.post_id = p.id LIMIT 1) as post_reddit_url
                FROM comments c
                JOIN posts p ON c.post_id = p.id
                LEFT JOIN subreddits s ON p.subreddit_id = s.id
                WHERE c.brand_id IN ({placeholders})"""
        p1 = list(brand_ids)
        if date_from:
            q1 += " AND c.created_at >= ?"
            p1.append(date_from)
        if date_to:
            q1 += " AND c.created_at <= ?"
            p1.append(date_to + " 23:59:59")

        # Search comments
        q2 = f"""SELECT sc.id, sc.body, sc.status, sc.account_id, sc.brand_id,
                       sc.is_reply, sc.mentions_brand, sc.created_at, sc.deployed_at,
                       sc.paid_at, sc.reddit_comment_url, 0 as suggested_post_day,
                       1 as is_ours, NULL as matched_keywords,
                       'search_comment' as source,
                       sp.title as post_title, NULL as subreddit_id,
                       sp.subreddit as subreddit_name,
                       sp.reddit_url as post_reddit_url
                FROM search_comments sc
                JOIN search_posts sp ON sc.search_post_id = sp.id
                WHERE sc.brand_id IN ({placeholders})"""
        p2 = list(brand_ids)
        if date_from:
            q2 += " AND sc.created_at >= ?"
            p2.append(date_from)
        if date_to:
            q2 += " AND sc.created_at <= ?"
            p2.append(date_to + " 23:59:59")

        query = f"SELECT * FROM ({q1} UNION ALL {q2}) combined ORDER BY created_at DESC, id DESC"
        rows = self.conn.execute(query, p1 + p2).fetchall()
        return [dict(r) for r in rows]

    def get_brand_subreddit_stats(self, brand_name, date_from=None, date_to=None):
        """Get per-subreddit stats for a brand: total comments, deployed, ours, brand mentions.

        Includes both regular comments and search_comments.
        Returns list of dicts: subreddit_id, subreddit_name, total, deployed, ours, mentions_brand, deleted.
        """
        date_clause_reg = ""
        date_clause_sc = ""
        p1 = [brand_name]
        p2 = [brand_name]
        if date_from:
            date_clause_reg += " AND c.created_at >= ?"
            date_clause_sc += " AND sc.created_at >= ?"
            p1.append(date_from)
            p2.append(date_from)
        if date_to:
            date_clause_reg += " AND c.created_at <= ?"
            date_clause_sc += " AND sc.created_at <= ?"
            p1.append(date_to + " 23:59:59")
            p2.append(date_to + " 23:59:59")

        # Regular comments
        q1 = f"""SELECT s.id as subreddit_id, s.name as subreddit_name,
                        c.status, c.is_ours, c.mentions_brand
                 FROM comments c
                 JOIN posts p ON c.post_id = p.id
                 JOIN brands b ON c.brand_id = b.id
                 JOIN subreddits s ON p.subreddit_id = s.id
                 WHERE LOWER(b.name) = LOWER(?) {date_clause_reg}"""

        # Search comments (use subreddit name from search_posts)
        q2 = f"""SELECT NULL as subreddit_id, sp.subreddit as subreddit_name,
                        sc.status, 1 as is_ours, sc.mentions_brand
                 FROM search_comments sc
                 JOIN search_posts sp ON sc.search_post_id = sp.id
                 JOIN brands b2 ON sc.brand_id = b2.id
                 WHERE LOWER(b2.name) = LOWER(?) {date_clause_sc}"""

        combined = f"{q1} UNION ALL {q2}"
        query = f"""SELECT subreddit_id, subreddit_name,
                          COUNT(*) as total,
                          SUM(CASE WHEN status = 'deployed' THEN 1 ELSE 0 END) as deployed,
                          SUM(CASE WHEN is_ours = 1 THEN 1 ELSE 0 END) as ours,
                          SUM(CASE WHEN mentions_brand = 1 THEN 1 ELSE 0 END) as mentions_brand,
                          SUM(CASE WHEN status = 'deleted' THEN 1 ELSE 0 END) as deleted,
                          SUM(CASE WHEN status IN ('assigned','informed') THEN 1 ELSE 0 END) as assigned,
                          SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) as complete
                   FROM ({combined})
                   GROUP BY subreddit_name
                   ORDER BY total DESC"""
        rows = self.conn.execute(query, p1 + p2).fetchall()
        return [dict(r) for r in rows]

    def get_unique_brand_names(self):
        """Get distinct brand names across all subreddits with aggregated info.
        Uses LEFT JOIN to include standalone brands (subreddit_id IS NULL)."""
        rows = self.conn.execute(
            """SELECT b.name,
                      GROUP_CONCAT(DISTINCT s.name) as subreddit_names,
                      b.domain_url, b.context, b.keywords,
                      COUNT(DISTINCT b.subreddit_id) as num_subreddits
               FROM brands b
               LEFT JOIN subreddits s ON b.subreddit_id = s.id
               GROUP BY LOWER(b.name)
               ORDER BY b.name"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_brand_overview_stats(self, brand_name, date_from=None, date_to=None):
        """Get aggregate stats for a brand across all subreddits (regular + search comments)."""
        date_clause_reg = ""
        date_clause_sc = ""
        p1 = [brand_name]
        p2 = [brand_name]
        if date_from:
            date_clause_reg += " AND c.created_at >= ?"
            date_clause_sc += " AND sc.created_at >= ?"
            p1.append(date_from)
            p2.append(date_from)
        if date_to:
            date_clause_reg += " AND c.created_at <= ?"
            date_clause_sc += " AND sc.created_at <= ?"
            p1.append(date_to + " 23:59:59")
            p2.append(date_to + " 23:59:59")

        # Regular comments
        q1 = f"""SELECT c.status, c.is_ours, c.mentions_brand, p.id as post_id, p.subreddit_id
                 FROM comments c
                 JOIN posts p ON c.post_id = p.id
                 JOIN brands b ON c.brand_id = b.id
                 WHERE LOWER(b.name) = LOWER(?) {date_clause_reg}"""

        # Search comments
        q2 = f"""SELECT sc.status, 1 as is_ours, sc.mentions_brand, sc.search_post_id as post_id, NULL as subreddit_id
                 FROM search_comments sc
                 JOIN brands b2 ON sc.brand_id = b2.id
                 WHERE LOWER(b2.name) = LOWER(?) {date_clause_sc}"""

        combined = f"SELECT * FROM ({q1} UNION ALL {q2}) combined"
        row = self.conn.execute(
            f"""SELECT COUNT(*) as total_comments,
                       SUM(CASE WHEN status = 'deployed' THEN 1 ELSE 0 END) as deployed,
                       SUM(CASE WHEN is_ours = 1 THEN 1 ELSE 0 END) as ours,
                       SUM(CASE WHEN mentions_brand = 1 THEN 1 ELSE 0 END) as mentions_brand,
                       SUM(CASE WHEN status = 'deleted' THEN 1 ELSE 0 END) as deleted,
                       SUM(CASE WHEN status IN ('assigned','informed') THEN 1 ELSE 0 END) as assigned,
                       SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) as complete,
                       COUNT(DISTINCT post_id) as total_posts,
                       COUNT(DISTINCT subreddit_id) as num_subreddits
                FROM ({combined})""",
            p1 + p2,
        ).fetchone()
        return dict(row) if row else {}

    def get_brand_deployed_hierarchy(self, brand_name):
        """Get deployed posts/comments for a brand, grouped by subreddit.

        Regular posts: subreddit → posts → branded/non-branded comments.
        Search comments: flat summary stats (no subreddit grouping).
        """
        brand_rows = self.conn.execute(
            "SELECT id, subreddit_id FROM brands WHERE LOWER(name) = LOWER(?)", (brand_name,)
        ).fetchall()
        brand_ids = [r["id"] for r in brand_rows]
        if not brand_ids:
            return {"brand_name": brand_name, "subreddits": [], "search_stats": {}}

        placeholders = ",".join("?" * len(brand_ids))

        # Regular deployed posts
        posts = [dict(r) for r in self.conn.execute(f"""
            SELECT DISTINCT p.id, p.title, p.status, p.owner_account, p.suggested_post_day,
                   p.subreddit_id, p.deployed_at, s.name as subreddit_name,
                   pu.reddit_url
            FROM posts p
            JOIN subreddits s ON p.subreddit_id = s.id
            LEFT JOIN post_urls pu ON pu.post_id = p.id
            LEFT JOIN post_brands pb ON pb.post_id = p.id
            WHERE (pb.brand_id IN ({placeholders}) OR p.brand_id IN ({placeholders}))
              AND p.status IN ('published', 'paid')
            ORDER BY s.name, p.suggested_post_day
        """, brand_ids + brand_ids).fetchall()]

        post_ids = [p["id"] for p in posts]

        # Deployed comments for those posts
        comments_by_post = {}
        if post_ids:
            pp = ",".join("?" * len(post_ids))
            for r in self.conn.execute(f"""
                SELECT c.id, c.post_id, c.account_id, c.mentions_brand,
                       c.deployed_at, c.reddit_comment_url
                FROM comments c
                WHERE c.post_id IN ({pp}) AND c.status = 'deployed'
                ORDER BY c.deployed_at
            """, post_ids).fetchall():
                row = dict(r)
                pid = row["post_id"]
                if pid not in comments_by_post:
                    comments_by_post[pid] = {"branded": [], "non_branded": []}
                bucket = "branded" if row.get("mentions_brand") else "non_branded"
                comments_by_post[pid][bucket].append(row)

        # Group regular posts by subreddit
        subs = {}
        for p in posts:
            sname = p["subreddit_name"]
            if sname not in subs:
                subs[sname] = {"name": sname, "subreddit_id": p["subreddit_id"], "posts": []}
            cm = comments_by_post.get(p["id"], {"branded": [], "non_branded": []})
            subs[sname]["posts"].append({
                "id": p["id"],
                "title": p["title"],
                "status": p["status"],
                "owner_account": p["owner_account"] or "",
                "reddit_url": p.get("reddit_url") or "",
                "deployed_at": p.get("deployed_at") or "",
                "suggested_post_day": p.get("suggested_post_day"),
                "branded_comments": cm["branded"],
                "non_branded_comments": cm["non_branded"],
            })

        # Search comments: flat stats only (no subreddit grouping)
        sc_row = self.conn.execute(f"""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN sc.mentions_brand = 1 THEN 1 ELSE 0 END) as branded,
                   SUM(CASE WHEN sc.mentions_brand = 0 OR sc.mentions_brand IS NULL THEN 1 ELSE 0 END) as non_branded,
                   COUNT(DISTINCT sc.account_id) as accounts_used,
                   COUNT(DISTINCT sp.subreddit) as subreddits
            FROM search_comments sc
            JOIN search_posts sp ON sc.search_post_id = sp.id
            WHERE sc.brand_id IN ({placeholders}) AND sc.status = 'deployed'
        """, brand_ids).fetchone()

        search_stats = dict(sc_row) if sc_row else {}

        return {
            "brand_name": brand_name,
            "subreddits": sorted(subs.values(), key=lambda s: s["name"]),
            "search_stats": search_stats,
        }

    # --- Accounts ---

    def create_account(self, username, link_karma=0, comment_karma=0, created_utc=None, reference=''):
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO accounts (username, link_karma, comment_karma, created_utc, reference, last_refreshed)
               VALUES (?, ?, ?, ?, ?, datetime('now'))""",
            (username, link_karma, comment_karma, created_utc, reference)
        )
        self.conn.commit()
        return cur.lastrowid

    def get_account(self, username):
        row = self.conn.execute("SELECT * FROM accounts WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    def list_accounts(self, min_karma=None, min_age_days=None, reference_search=None):
        query = """SELECT *,
                          (SELECT COUNT(*) FROM subreddits WHERE owner_account = accounts.username) as owned_subreddits,
                          (SELECT COUNT(*) FROM posts WHERE owner_account = accounts.username) as owned_posts
                   FROM accounts WHERE 1=1"""
        params = []
        if min_karma is not None:
            query += " AND (link_karma + comment_karma) >= ?"
            params.append(int(min_karma))
        if min_age_days is not None:
            query += " AND created_utc IS NOT NULL AND created_utc <= (strftime('%s','now') - ? * 86400)"
            params.append(int(min_age_days))
        if reference_search:
            query += " AND reference LIKE '%' || ? || '%'"
            params.append(reference_search)
        query += " ORDER BY username"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def update_account_reddit_data(self, username, link_karma, comment_karma, created_utc):
        self.conn.execute(
            """UPDATE accounts
               SET link_karma = ?, comment_karma = ?, created_utc = ?,
                   last_refreshed = datetime('now'),
                   last_refresh_attempt = datetime('now'),
                   last_refresh_error = NULL
               WHERE username = ?""",
            (link_karma, comment_karma, created_utc, username)
        )
        self.conn.commit()

    def record_refresh_failure(self, username, error_msg):
        """Mark a refresh attempt as failed (last_refreshed stays untouched)."""
        self.conn.execute(
            """UPDATE accounts
               SET last_refresh_attempt = datetime('now'),
                   last_refresh_error = ?
               WHERE username = ?""",
            ((error_msg or "")[:500], username)
        )
        self.conn.commit()

    # --- Global lifetime rotation counter (one number per account) ---

    def _increment_lifetime(self, username):
        """Bump the global rotation counter when an account is assigned something.
        Called from every assignment path — comments, search comments, post-ownership.
        Caller is responsible for commit (this method does not commit on its own)."""
        if not username:
            return
        self.conn.execute(
            "UPDATE accounts SET lifetime_assignments = lifetime_assignments + 1 WHERE username = ?",
            (username,)
        )

    def bump_assign_seq(self, username):
        """Set assign_seq to current max + 1 (global monotonic).
        Ensures this account is last in round-robin order."""
        if not username:
            return
        max_seq = self.conn.execute("SELECT COALESCE(MAX(assign_seq), 0) FROM accounts").fetchone()[0]
        self.conn.execute("UPDATE accounts SET assign_seq = ? WHERE username = ?", (max_seq + 1, username))
        self.conn.commit()

    def _decrement_lifetime(self, username, n=1):
        """Roll the global rotation counter back when an account is unassigned or
        its assigned row is deleted. Floors at 0 so the counter cannot go negative."""
        if not username or n <= 0:
            return
        self.conn.execute(
            "UPDATE accounts SET lifetime_assignments = MAX(0, lifetime_assignments - ?) WHERE username = ?",
            (n, username)
        )

    # --- app_meta helpers (one-time flags for startup tasks) ---

    def meta_get(self, key):
        row = self.conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def meta_set(self, key, value):
        self.conn.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value))
        )
        self.conn.commit()

    def claim_periodic(self, key, interval_hours):
        """Atomically claim a periodic job. Returns True iff at least `interval_hours` have
        elapsed since the last claim stored under `key` (and stamps 'now' as the new value).

        Safe across gunicorn workers / restarts: the conditional UPDATE runs under SQLite's
        single-writer lock, so when several workers race, exactly ONE sees `value <= threshold`
        and flips it — the rest get rowcount 0. Timestamps are ISO 'YYYY-MM-DD HH:MM:SS'
        (UTC), which sorts chronologically so the lexical `<=` compare is correct."""
        from datetime import datetime, timedelta
        try:
            hrs = float(interval_hours)
        except (TypeError, ValueError):
            hrs = 48.0
        now = datetime.utcnow()
        now_s = now.strftime("%Y-%m-%d %H:%M:%S")
        thresh_s = (now - timedelta(hours=hrs)).strftime("%Y-%m-%d %H:%M:%S")
        # Ensure the row exists (sentinel epoch → first call always claims).
        self.conn.execute(
            "INSERT OR IGNORE INTO app_meta (key, value) VALUES (?, '1970-01-01 00:00:00')",
            (key,))
        cur = self.conn.execute(
            "UPDATE app_meta SET value = ? WHERE key = ? AND value <= ?",
            (now_s, key, thresh_s))
        self.conn.commit()
        return cur.rowcount == 1

    def list_stale_accounts(self):
        """Return usernames of accounts that look like they need a (re)fresh:
        never refreshed, stale >7 days, last refresh errored, or total karma < 10
        (a strong signal of a prior silently-failed refresh)."""
        rows = self.conn.execute(
            """SELECT username FROM accounts
               WHERE last_refreshed IS NULL
                  OR last_refreshed < datetime('now','-7 days')
                  OR last_refresh_error IS NOT NULL
                  OR (COALESCE(link_karma,0) + COALESCE(comment_karma,0)) < 10
               ORDER BY username"""
        ).fetchall()
        return [r["username"] for r in rows]

    def update_account_reference(self, username, reference):
        self.conn.execute("UPDATE accounts SET reference = ? WHERE username = ?", (reference, username))
        self.conn.commit()

    def toggle_account_excluded(self, username):
        """Toggle the excluded flag on an account. Returns new excluded value."""
        row = self.conn.execute("SELECT excluded FROM accounts WHERE username = ?", (username,)).fetchone()
        if not row:
            return None
        new_val = 0 if row["excluded"] else 1
        self.conn.execute("UPDATE accounts SET excluded = ? WHERE username = ?", (new_val, username))
        self.conn.commit()
        return new_val

    def delete_account(self, username):
        # Adjust lifetime counter before clearing assignments
        comment_cnt = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM comments WHERE account_id = ? AND status NOT IN ('draft','complete')",
            (username,)
        ).fetchone()["cnt"]
        search_cnt = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM search_comments WHERE account_id = ? AND status NOT IN ('draft','deleted')",
            (username,)
        ).fetchone()["cnt"]
        post_cnt = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM posts WHERE owner_account = ?",
            (username,)
        ).fetchone()["cnt"]
        total = comment_cnt + search_cnt + post_cnt
        if total > 0:
            self._decrement_lifetime(username, total)
        # Unassign comments owned by this account (back to draft)
        self.conn.execute(
            "UPDATE comments SET account_id = NULL, status = 'draft' WHERE account_id = ? AND status IN ('assigned','informed')",
            (username,)
        )
        # Clear account_id on deployed/complete comments (keep their status)
        self.conn.execute(
            "UPDATE comments SET account_id = NULL WHERE account_id = ? AND status NOT IN ('assigned','informed')",
            (username,)
        )
        # Same for search_comments
        self.conn.execute(
            "UPDATE search_comments SET account_id = NULL, status = 'draft' WHERE account_id = ? AND status IN ('assigned','informed')",
            (username,)
        )
        self.conn.execute(
            "UPDATE search_comments SET account_id = NULL WHERE account_id = ? AND status NOT IN ('assigned','informed')",
            (username,)
        )
        # Clear owner_account on posts and subreddits
        self.conn.execute("UPDATE posts SET owner_account = '' WHERE owner_account = ?", (username,))
        self.conn.execute("UPDATE subreddits SET owner_account = '' WHERE owner_account = ?", (username,))
        # Delete the account
        self.conn.execute("DELETE FROM accounts WHERE username = ?", (username,))
        self.conn.commit()

    def get_accounts_with_assignment_counts(self):
        rows = self.conn.execute(
            """SELECT a.*,
                      COUNT(c.id) as total_assigned,
                      SUM(CASE WHEN c.status IN ('assigned','informed') THEN 1 ELSE 0 END) as pending,
                      SUM(CASE WHEN c.status = 'deployed' THEN 1 ELSE 0 END) as deployed,
                      SUM(CASE WHEN c.status = 'deleted' THEN 1 ELSE 0 END) as deleted,
                      SUM(CASE WHEN c.status = 'paid' THEN 1 ELSE 0 END) as paid,
                      SUM(CASE WHEN c.status = 'deployed' AND c.deployed_at < datetime('now', '-4 days') AND c.deleted_at IS NULL THEN 1 ELSE 0 END) as due_payment,
                      (SELECT COUNT(*) FROM subreddits WHERE owner_account = a.username) as owned_subreddits,
                      (SELECT COUNT(*) FROM posts WHERE owner_account = a.username) as owned_posts
               FROM accounts a
               LEFT JOIN comments c ON c.account_id = a.username
               GROUP BY a.id
               ORDER BY a.username"""
        ).fetchall()
        return [dict(r) for r in rows]

    def set_subreddit_owner(self, subreddit_id, username):
        self.conn.execute("UPDATE subreddits SET owner_account = ? WHERE id = ?", (username, subreddit_id))
        self.conn.commit()

    def set_post_owner(self, post_id, username):
        row = self.conn.execute(
            "SELECT owner_account, status FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        prior = row["owner_account"] if row else None
        self.conn.execute("UPDATE posts SET owner_account = ? WHERE id = ?", (username, post_id))
        # Lifecycle: assigning an owner advances a pre-assignment post to 'assigned'
        # (mirrors the comment lifecycle). Guarded to draft/complete so we NEVER
        # downgrade an informed/deployed/paid/report/removed post — important because
        # the publish flow and reassignment both call this on already-live posts.
        if username and row and row["status"] in ("draft", "complete"):
            self.conn.execute("UPDATE posts SET status = 'assigned' WHERE id = ?", (post_id,))
        if prior and prior != username:
            self._decrement_lifetime(prior)
        if username and username != prior:
            self._increment_lifetime(username)
        self.conn.commit()

    def get_post_auto_assign_context(self, subreddit_id):
        """Fetch all data needed for post auto-assignment scoring."""
        sub = self.conn.execute("SELECT * FROM subreddits WHERE id = ?", (subreddit_id,)).fetchone()
        if not sub:
            return None

        draft_posts = [dict(r) for r in self.conn.execute(
            "SELECT * FROM posts WHERE subreddit_id = ? AND (owner_account IS NULL OR owner_account = '')",
            (subreddit_id,)
        ).fetchall()]

        all_accounts = [dict(r) for r in self.conn.execute(
            "SELECT * FROM accounts WHERE excluded = 0 ORDER BY username"
        ).fetchall()]

        # Per-account count of posts they already own across ALL subreddits
        account_post_counts = [dict(r) for r in self.conn.execute(
            """SELECT owner_account as account_id, COUNT(*) as cnt
               FROM posts WHERE owner_account IS NOT NULL AND owner_account != ''
               GROUP BY owner_account"""
        ).fetchall()]

        # Per-account count of posts they own in THIS subreddit
        account_sub_post_counts = [dict(r) for r in self.conn.execute(
            """SELECT owner_account as account_id, COUNT(*) as cnt
               FROM posts WHERE subreddit_id = ? AND owner_account IS NOT NULL AND owner_account != ''
               GROUP BY owner_account""",
            (subreddit_id,)
        ).fetchall()]

        # Per-account count of comments assigned in this subreddit (shows activity)
        # Cross-table: includes search_comments by matching subreddit name
        sub_name = dict(sub).get("name", "")
        account_sub_comment_counts = [dict(r) for r in self.conn.execute(
            """SELECT account_id, SUM(cnt) as cnt FROM (
                   SELECT c.account_id, COUNT(*) as cnt
                   FROM comments c JOIN posts p ON c.post_id = p.id
                   WHERE p.subreddit_id = ? AND c.account_id IS NOT NULL
                   GROUP BY c.account_id
                   UNION ALL
                   SELECT sc.account_id, COUNT(*) as cnt
                   FROM search_comments sc JOIN search_posts sp ON sc.search_post_id = sp.id
                   WHERE sp.subreddit = ? AND sc.account_id IS NOT NULL
                   GROUP BY sc.account_id
               ) GROUP BY account_id""",
            (subreddit_id, sub_name)
        ).fetchall()]

        # Cross-table: pending comment counts (assigned+informed) across both tables
        account_pending_counts = [dict(r) for r in self.conn.execute(
            """SELECT account_id, SUM(cnt) as cnt FROM (
                   SELECT account_id, COUNT(*) as cnt FROM comments
                   WHERE status IN ('assigned','informed') AND account_id IS NOT NULL
                   GROUP BY account_id
                   UNION ALL
                   SELECT account_id, COUNT(*) as cnt FROM search_comments
                   WHERE status IN ('assigned','informed') AND account_id IS NOT NULL
                   GROUP BY account_id
               ) GROUP BY account_id"""
        ).fetchall()]

        # Cross-table: deployed comment counts across both tables
        account_deployed_counts = [dict(r) for r in self.conn.execute(
            """SELECT account_id, SUM(cnt) as cnt FROM (
                   SELECT account_id, COUNT(*) as cnt FROM comments
                   WHERE status IN ('deployed', 'removed') AND account_id IS NOT NULL
                   GROUP BY account_id
                   UNION ALL
                   SELECT account_id, COUNT(*) as cnt FROM search_comments
                   WHERE status IN ('deployed', 'removed') AND account_id IS NOT NULL
                   GROUP BY account_id
               ) GROUP BY account_id"""
        ).fetchall()]

        return {
            "subreddit": dict(sub),
            "draft_posts": draft_posts,
            "all_accounts": all_accounts,
            "account_post_counts": account_post_counts,
            "account_sub_post_counts": account_sub_post_counts,
            "account_sub_comment_counts": account_sub_comment_counts,
            "account_pending_counts": account_pending_counts,
            "account_deployed_counts": account_deployed_counts,
        }

    def bulk_unassign_posts_in_subreddit(self, subreddit_id):
        """Remove owner_account from posts in a subreddit. Skips informed, published (deployed)."""
        # Collect affected (owner_account, cnt) first so the lifetime counter can
        # be rolled back for every freed owner slot.
        freed = self.conn.execute(
            """SELECT owner_account, COUNT(*) AS cnt FROM posts
               WHERE subreddit_id = ?
                 AND status NOT IN ('published', 'informed')
                 AND owner_account IS NOT NULL AND owner_account != ''
               GROUP BY owner_account""",
            (subreddit_id,)
        ).fetchall()
        cur = self.conn.execute(
            "UPDATE posts SET owner_account = '', "
            "status = CASE WHEN status = 'assigned' THEN 'complete' ELSE status END "
            "WHERE subreddit_id = ? AND status NOT IN ('published', 'informed') "
            "AND owner_account IS NOT NULL AND owner_account != ''",
            (subreddit_id,)
        )
        for r in freed:
            self._decrement_lifetime(r["owner_account"], r["cnt"])
        self.conn.commit()
        return cur.rowcount

    def bulk_unassign_comments_in_subreddit(self, subreddit_id):
        """Unassign all assigned comments across all posts in a subreddit. Skips informed and deployed."""
        freed = self.conn.execute(
            """SELECT account_id, COUNT(*) AS cnt FROM comments
               WHERE post_id IN (SELECT id FROM posts WHERE subreddit_id = ?)
                 AND status = 'assigned' AND account_id IS NOT NULL
               GROUP BY account_id""",
            (subreddit_id,)
        ).fetchall()
        cur = self.conn.execute(
            """UPDATE comments SET account_id = NULL, status = 'draft'
               WHERE post_id IN (SELECT id FROM posts WHERE subreddit_id = ?)
                 AND status = 'assigned'""",
            (subreddit_id,)
        )
        for r in freed:
            self._decrement_lifetime(r["account_id"], r["cnt"])
        self.conn.commit()
        return cur.rowcount

    def bulk_unassign_post_comments(self, post_id):
        """Unassign all assigned comments for a post, setting them back to draft. Skips informed and deployed."""
        freed = self.conn.execute(
            """SELECT account_id, COUNT(*) AS cnt FROM comments
               WHERE post_id = ? AND status = 'assigned' AND account_id IS NOT NULL
               GROUP BY account_id""",
            (post_id,)
        ).fetchall()
        self.conn.execute(
            "UPDATE comments SET account_id = NULL, status = 'draft' WHERE post_id = ? AND status = 'assigned'",
            (post_id,)
        )
        for r in freed:
            self._decrement_lifetime(r["account_id"], r["cnt"])
        self.conn.commit()

    def unassign_post_owner(self, post_id):
        """Remove owner_account from a post."""
        row = self.conn.execute(
            "SELECT owner_account, status FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        prior = row["owner_account"] if row else None
        self.conn.execute("UPDATE posts SET owner_account = '' WHERE id = ?", (post_id,))
        # Revert the owner-driven lifecycle bump: only an 'assigned' post goes back to
        # 'complete'. Leave informed/deployed/paid/report/removed untouched.
        if row and row["status"] == "assigned":
            self.conn.execute("UPDATE posts SET status = 'complete' WHERE id = ?", (post_id,))
        if prior:
            self._decrement_lifetime(prior)
        self.conn.commit()

    def bulk_unassign_all_for_post(self, post_id):
        """Unassign post owner + all assigned comments for a post."""
        self.unassign_post_owner(post_id)
        self.bulk_unassign_post_comments(post_id)

    def get_auto_assign_context(self, post_id):
        """Fetch all data needed for auto-assignment scoring in one call."""
        post = self.get_post(post_id)
        if not post:
            return None

        sub_id = post["subreddit_id"]

        draft_comments = [dict(r) for r in self.conn.execute(
            """SELECT * FROM comments
               WHERE post_id = ? AND status IN ('draft', 'complete') AND (account_id IS NULL OR account_id = '')
               ORDER BY comment_type = 'op_reply' DESC, mentions_brand DESC, suggested_post_day, suggested_order""",
            (post_id,)
        ).fetchall()]

        all_accounts = [dict(r) for r in self.conn.execute(
            "SELECT * FROM accounts WHERE excluded = 0 ORDER BY username"
        ).fetchall()]

        # Cross-table: subreddit day assignments from BOTH comments and search_comments
        sub_name = self.conn.execute("SELECT name FROM subreddits WHERE id = ?", (sub_id,)).fetchone()
        sub_name_val = sub_name["name"] if sub_name else ""
        subreddit_day_assignments = [dict(r) for r in self.conn.execute(
            """SELECT account_id, suggested_post_day, SUM(cnt) as cnt FROM (
                   SELECT c.account_id, c.suggested_post_day, COUNT(*) as cnt
                   FROM comments c JOIN posts p ON c.post_id = p.id
                   WHERE p.subreddit_id = ? AND c.account_id IS NOT NULL
                     AND c.status IN ('assigned','informed','deployed','paid')
                     AND (c.deployed_at IS NULL OR c.deployed_at > datetime('now', '-30 days'))
                   GROUP BY c.account_id, c.suggested_post_day
                   UNION ALL
                   SELECT sc.account_id, 0 as suggested_post_day, COUNT(*) as cnt
                   FROM search_comments sc JOIN search_posts sp ON sc.search_post_id = sp.id
                   WHERE sp.subreddit = ? AND sc.account_id IS NOT NULL
                     AND sc.status IN ('assigned','informed','deployed','paid')
                     AND (sc.deployed_at IS NULL OR sc.deployed_at > datetime('now', '-30 days'))
                   GROUP BY sc.account_id
               ) GROUP BY account_id, suggested_post_day""",
            (sub_id, sub_name_val)
        ).fetchall()]

        # Cross-table: pending counts from BOTH comments and search_comments
        account_pending_counts = [dict(r) for r in self.conn.execute(
            """SELECT account_id, SUM(cnt) as cnt FROM (
                   SELECT account_id, COUNT(*) as cnt FROM comments
                   WHERE status IN ('assigned','informed') AND account_id IS NOT NULL
                   GROUP BY account_id
                   UNION ALL
                   SELECT account_id, COUNT(*) as cnt FROM search_comments
                   WHERE status IN ('assigned','informed') AND account_id IS NOT NULL
                   GROUP BY account_id
               ) GROUP BY account_id"""
        ).fetchall()]

        # Cross-table: brand mentions from BOTH tables
        account_brand_mentions = [dict(r) for r in self.conn.execute(
            """SELECT account_id, brand_id, SUM(cnt) as cnt FROM (
                   SELECT account_id, brand_id, COUNT(*) as cnt FROM comments
                   WHERE mentions_brand = 1 AND account_id IS NOT NULL
                   GROUP BY account_id, brand_id
                   UNION ALL
                   SELECT account_id, brand_id, COUNT(*) as cnt FROM search_comments
                   WHERE mentions_brand = 1 AND account_id IS NOT NULL
                   GROUP BY account_id, brand_id
               ) GROUP BY account_id, brand_id"""
        ).fetchall()]

        # Cross-table: total brand mentions from BOTH tables
        account_total_mentions = [dict(r) for r in self.conn.execute(
            """SELECT account_id, SUM(cnt) as cnt FROM (
                   SELECT account_id, COUNT(*) as cnt FROM comments
                   WHERE mentions_brand = 1 AND account_id IS NOT NULL
                   GROUP BY account_id
                   UNION ALL
                   SELECT account_id, COUNT(*) as cnt FROM search_comments
                   WHERE mentions_brand = 1 AND account_id IS NOT NULL
                   GROUP BY account_id
               ) GROUP BY account_id"""
        ).fetchall()]

        # subreddit_veterans: removed (unused by score_account). Kept as empty
        # set for backward compat with _build_lookups.
        subreddit_veterans = []

        # Global deployed footprint: total deployed comments + search_comments per account
        account_deployed_counts = [dict(r) for r in self.conn.execute(
            """SELECT account_id, SUM(cnt) as cnt FROM (
                   SELECT account_id, COUNT(*) as cnt FROM comments
                   WHERE status IN ('deployed', 'removed') AND account_id IS NOT NULL
                   GROUP BY account_id
                   UNION ALL
                   SELECT account_id, COUNT(*) as cnt FROM search_comments
                   WHERE status IN ('deployed', 'removed') AND account_id IS NOT NULL
                   GROUP BY account_id
               ) GROUP BY account_id"""
        ).fetchall()]

        # Subreddit-specific deployed count per account (for familiarity bonus)
        subreddit_deployed_counts = [dict(r) for r in self.conn.execute(
            """SELECT account_id, SUM(cnt) as cnt FROM (
                   SELECT c.account_id, COUNT(*) as cnt FROM comments c
                   JOIN posts p ON c.post_id = p.id
                   WHERE p.subreddit_id = ? AND c.account_id IS NOT NULL
                     AND c.status = 'deployed'
                   GROUP BY c.account_id
                   UNION ALL
                   SELECT sc.account_id, COUNT(*) as cnt FROM search_comments sc
                   JOIN search_posts sp ON sc.search_post_id = sp.id
                   WHERE sp.subreddit = ? AND sc.account_id IS NOT NULL
                     AND sc.status = 'deployed'
                   GROUP BY sc.account_id
               ) GROUP BY account_id""",
            (sub_id, sub_name_val)
        ).fetchall()]

        # Post ownership count per account (all subreddits)
        account_post_ownership = [dict(r) for r in self.conn.execute(
            """SELECT owner_account as account_id, COUNT(*) as cnt FROM posts
               WHERE owner_account IS NOT NULL AND owner_account != ''
               GROUP BY owner_account"""
        ).fetchall()]

        # Number of distinct subreddits each account is active in (comments + search_comments)
        account_sub_spread = [dict(r) for r in self.conn.execute(
            """SELECT account_id, COUNT(DISTINCT sub) as cnt FROM (
                   SELECT c.account_id, p.subreddit_id as sub FROM comments c
                   JOIN posts p ON c.post_id = p.id
                   WHERE c.account_id IS NOT NULL AND c.status IN ('assigned','informed','deployed','paid')
                   UNION ALL
                   SELECT sc.account_id, sp.subreddit as sub FROM search_comments sc
                   JOIN search_posts sp ON sc.search_post_id = sp.id
                   WHERE sc.account_id IS NOT NULL AND sc.status IN ('assigned','informed','deployed','paid')
               ) GROUP BY account_id"""
        ).fetchall()]

        return {
            "post": post,
            "draft_comments": draft_comments,
            "all_accounts": all_accounts,
            "subreddit_day_assignments": subreddit_day_assignments,
            "account_pending_counts": account_pending_counts,
            "account_brand_mentions": account_brand_mentions,
            "account_total_mentions": account_total_mentions,
            "subreddit_veterans": set(subreddit_veterans),
            "account_deployed_counts": account_deployed_counts,
            "subreddit_deployed_counts": subreddit_deployed_counts,
            "account_post_ownership": account_post_ownership,
            "account_sub_spread": account_sub_spread,
        }

    def get_search_auto_assign_context(self):
        """Fetch all data needed for auto-assignment of search comments."""
        # Draft search comments needing assignment
        draft_comments = [dict(r) for r in self.conn.execute(
            """SELECT sc.*, sp.subreddit, sp.title as post_title, sp.reddit_url
               FROM search_comments sc
               JOIN search_posts sp ON sc.search_post_id = sp.id
               WHERE sc.status = 'draft' AND (sc.account_id IS NULL OR sc.account_id = '')
               ORDER BY sc.search_post_id, sc.mentions_brand DESC"""
        ).fetchall()]

        if not draft_comments:
            return {"draft_comments": [], "all_accounts": [], "subreddit_day_assignments": [],
                    "account_pending_counts": [], "account_brand_mentions": [],
                    "account_total_mentions": [], "subreddit_veterans": set(),
                    "account_deployed_counts": [], "subreddit_deployed_counts": [],
                    "account_post_ownership": [], "account_sub_spread": []}

        all_accounts = [dict(r) for r in self.conn.execute(
            "SELECT * FROM accounts WHERE excluded = 0 ORDER BY username"
        ).fetchall()]

        # Distinct subreddits in the draft batch
        sub_names = list(set(c["subreddit"] for c in draft_comments if c.get("subreddit")))
        placeholders = ",".join("?" * len(sub_names))

        # Subreddit day assignments: both regular comments (via subreddits.name) and search_comments
        subreddit_day_assignments = [dict(r) for r in self.conn.execute(
            f"""SELECT account_id, suggested_post_day, SUM(cnt) as cnt FROM (
                   SELECT c.account_id, c.suggested_post_day, COUNT(*) as cnt
                   FROM comments c JOIN posts p ON c.post_id = p.id
                   JOIN subreddits s ON p.subreddit_id = s.id
                   WHERE s.name IN ({placeholders}) AND c.account_id IS NOT NULL
                     AND c.status IN ('assigned','informed','deployed','paid')
                     AND (c.deployed_at IS NULL OR c.deployed_at > datetime('now', '-30 days'))
                   GROUP BY c.account_id, c.suggested_post_day
                   UNION ALL
                   SELECT sc.account_id, 0 as suggested_post_day, COUNT(*) as cnt
                   FROM search_comments sc JOIN search_posts sp ON sc.search_post_id = sp.id
                   WHERE sp.subreddit IN ({placeholders}) AND sc.account_id IS NOT NULL
                     AND sc.status IN ('assigned','informed','deployed','paid')
                     AND (sc.deployed_at IS NULL OR sc.deployed_at > datetime('now', '-30 days'))
                   GROUP BY sc.account_id
               ) GROUP BY account_id, suggested_post_day""",
            sub_names + sub_names
        ).fetchall()]

        # Global pending counts (same as regular - subreddit agnostic)
        account_pending_counts = [dict(r) for r in self.conn.execute(
            """SELECT account_id, SUM(cnt) as cnt FROM (
                   SELECT account_id, COUNT(*) as cnt FROM comments
                   WHERE status IN ('assigned','informed') AND account_id IS NOT NULL
                   GROUP BY account_id
                   UNION ALL
                   SELECT account_id, COUNT(*) as cnt FROM search_comments
                   WHERE status IN ('assigned','informed') AND account_id IS NOT NULL
                   GROUP BY account_id
               ) GROUP BY account_id"""
        ).fetchall()]

        account_brand_mentions = [dict(r) for r in self.conn.execute(
            """SELECT account_id, brand_id, SUM(cnt) as cnt FROM (
                   SELECT account_id, brand_id, COUNT(*) as cnt FROM comments
                   WHERE mentions_brand = 1 AND account_id IS NOT NULL
                   GROUP BY account_id, brand_id
                   UNION ALL
                   SELECT account_id, brand_id, COUNT(*) as cnt FROM search_comments
                   WHERE mentions_brand = 1 AND account_id IS NOT NULL
                   GROUP BY account_id, brand_id
               ) GROUP BY account_id, brand_id"""
        ).fetchall()]

        account_total_mentions = [dict(r) for r in self.conn.execute(
            """SELECT account_id, SUM(cnt) as cnt FROM (
                   SELECT account_id, COUNT(*) as cnt FROM comments
                   WHERE mentions_brand = 1 AND account_id IS NOT NULL
                   GROUP BY account_id
                   UNION ALL
                   SELECT account_id, COUNT(*) as cnt FROM search_comments
                   WHERE mentions_brand = 1 AND account_id IS NOT NULL
                   GROUP BY account_id
               ) GROUP BY account_id"""
        ).fetchall()]

        # subreddit_veterans: removed (unused by score_account, was an expensive
        # full-table scan). Kept as empty set for backward compat with _build_lookups.
        subreddit_veterans = []

        account_deployed_counts = [dict(r) for r in self.conn.execute(
            """SELECT account_id, SUM(cnt) as cnt FROM (
                   SELECT account_id, COUNT(*) as cnt FROM comments
                   WHERE status IN ('deployed', 'removed') AND account_id IS NOT NULL
                   GROUP BY account_id
                   UNION ALL
                   SELECT account_id, COUNT(*) as cnt FROM search_comments
                   WHERE status IN ('deployed', 'removed') AND account_id IS NOT NULL
                   GROUP BY account_id
               ) GROUP BY account_id"""
        ).fetchall()]

        account_post_ownership = [dict(r) for r in self.conn.execute(
            """SELECT owner_account as account_id, COUNT(*) as cnt FROM posts
               WHERE owner_account IS NOT NULL AND owner_account != ''
               GROUP BY owner_account"""
        ).fetchall()]

        account_sub_spread = [dict(r) for r in self.conn.execute(
            """SELECT account_id, COUNT(DISTINCT sub) as cnt FROM (
                   SELECT c.account_id, p.subreddit_id as sub FROM comments c
                   JOIN posts p ON c.post_id = p.id
                   WHERE c.account_id IS NOT NULL AND c.status IN ('assigned','informed','deployed','paid')
                   UNION ALL
                   SELECT sc.account_id, sp.subreddit as sub FROM search_comments sc
                   JOIN search_posts sp ON sc.search_post_id = sp.id
                   WHERE sc.account_id IS NOT NULL AND sc.status IN ('assigned','informed','deployed','paid')
               ) GROUP BY account_id"""
        ).fetchall()]

        # Subreddit-specific deployed count per account (for familiarity bonus)
        # For search comments, we query across all subreddits in the batch
        subreddit_deployed_counts = [dict(r) for r in self.conn.execute(
            f"""SELECT account_id, SUM(cnt) as cnt FROM (
                   SELECT c.account_id, COUNT(*) as cnt FROM comments c
                   JOIN posts p ON c.post_id = p.id
                   JOIN subreddits s ON p.subreddit_id = s.id
                   WHERE s.name IN ({placeholders}) AND c.account_id IS NOT NULL
                     AND c.status = 'deployed'
                   GROUP BY c.account_id
                   UNION ALL
                   SELECT sc.account_id, COUNT(*) as cnt FROM search_comments sc
                   JOIN search_posts sp ON sc.search_post_id = sp.id
                   WHERE sp.subreddit IN ({placeholders}) AND sc.account_id IS NOT NULL
                     AND sc.status = 'deployed'
                   GROUP BY sc.account_id
               ) GROUP BY account_id""",
            sub_names + sub_names
        ).fetchall()]

        return {
            "draft_comments": draft_comments,
            "all_accounts": all_accounts,
            "subreddit_day_assignments": subreddit_day_assignments,
            "account_pending_counts": account_pending_counts,
            "account_brand_mentions": account_brand_mentions,
            "account_total_mentions": account_total_mentions,
            "subreddit_veterans": set(subreddit_veterans),
            "account_deployed_counts": account_deployed_counts,
            "subreddit_deployed_counts": subreddit_deployed_counts,
            "account_post_ownership": account_post_ownership,
            "account_sub_spread": account_sub_spread,
        }

    def get_single_search_comment_context(self, comment_id):
        """Lightweight context fetch for auto-assigning ONE search comment.

        Avoids the whole-batch scans in get_search_auto_assign_context():
          - Skips draft_comments query (we only need one target)
          - Scopes subreddit-specific lookups to the single target subreddit
          - Scopes brand_mentions to the single target brand_id only
        Much faster than the full context function.
        """
        target = self.conn.execute(
            """SELECT sc.*, sp.subreddit, sp.title as post_title, sp.reddit_url
               FROM search_comments sc
               JOIN search_posts sp ON sc.search_post_id = sp.id
               WHERE sc.id = ?""", (comment_id,)
        ).fetchone()
        if not target:
            return None
        target = dict(target)
        sub_name = target.get("subreddit") or ""
        target_brand_id = target.get("brand_id")

        all_accounts = [dict(r) for r in self.conn.execute(
            "SELECT * FROM accounts WHERE excluded = 0 ORDER BY username"
        ).fetchall()]

        # Subreddit day assignments — scoped to the single target subreddit
        subreddit_day_assignments = [dict(r) for r in self.conn.execute(
            """SELECT account_id, suggested_post_day, SUM(cnt) as cnt FROM (
                   SELECT c.account_id, c.suggested_post_day, COUNT(*) as cnt
                   FROM comments c JOIN posts p ON c.post_id = p.id
                   JOIN subreddits s ON p.subreddit_id = s.id
                   WHERE s.name = ? AND c.account_id IS NOT NULL
                     AND c.status IN ('assigned','informed','deployed','paid')
                     AND (c.deployed_at IS NULL OR c.deployed_at > datetime('now', '-30 days'))
                   GROUP BY c.account_id, c.suggested_post_day
                   UNION ALL
                   SELECT sc.account_id, 0 as suggested_post_day, COUNT(*) as cnt
                   FROM search_comments sc JOIN search_posts sp ON sc.search_post_id = sp.id
                   WHERE sp.subreddit = ? AND sc.account_id IS NOT NULL
                     AND sc.status IN ('assigned','informed','deployed','paid')
                     AND (sc.deployed_at IS NULL OR sc.deployed_at > datetime('now', '-30 days'))
                   GROUP BY sc.account_id
               ) GROUP BY account_id, suggested_post_day""",
            (sub_name, sub_name)
        ).fetchall()]

        # Global pending counts
        account_pending_counts = [dict(r) for r in self.conn.execute(
            """SELECT account_id, SUM(cnt) as cnt FROM (
                   SELECT account_id, COUNT(*) as cnt FROM comments
                   WHERE status IN ('assigned','informed') AND account_id IS NOT NULL
                   GROUP BY account_id
                   UNION ALL
                   SELECT account_id, COUNT(*) as cnt FROM search_comments
                   WHERE status IN ('assigned','informed') AND account_id IS NOT NULL
                   GROUP BY account_id
               ) GROUP BY account_id"""
        ).fetchall()]

        # Brand mentions — scoped to the target brand only (score_account only
        # looks up lookups["brand_mentions"][user][target_brand_id]).
        if target_brand_id is not None:
            account_brand_mentions = [dict(r) for r in self.conn.execute(
                """SELECT account_id, brand_id, SUM(cnt) as cnt FROM (
                       SELECT account_id, brand_id, COUNT(*) as cnt FROM comments
                       WHERE mentions_brand = 1 AND brand_id = ? AND account_id IS NOT NULL
                       GROUP BY account_id, brand_id
                       UNION ALL
                       SELECT account_id, brand_id, COUNT(*) as cnt FROM search_comments
                       WHERE mentions_brand = 1 AND brand_id = ? AND account_id IS NOT NULL
                       GROUP BY account_id, brand_id
                   ) GROUP BY account_id, brand_id""",
                (target_brand_id, target_brand_id)
            ).fetchall()]
        else:
            account_brand_mentions = []

        account_total_mentions = [dict(r) for r in self.conn.execute(
            """SELECT account_id, SUM(cnt) as cnt FROM (
                   SELECT account_id, COUNT(*) as cnt FROM comments
                   WHERE mentions_brand = 1 AND account_id IS NOT NULL
                   GROUP BY account_id
                   UNION ALL
                   SELECT account_id, COUNT(*) as cnt FROM search_comments
                   WHERE mentions_brand = 1 AND account_id IS NOT NULL
                   GROUP BY account_id
               ) GROUP BY account_id"""
        ).fetchall()]

        account_deployed_counts = [dict(r) for r in self.conn.execute(
            """SELECT account_id, SUM(cnt) as cnt FROM (
                   SELECT account_id, COUNT(*) as cnt FROM comments
                   WHERE status IN ('deployed', 'removed') AND account_id IS NOT NULL
                   GROUP BY account_id
                   UNION ALL
                   SELECT account_id, COUNT(*) as cnt FROM search_comments
                   WHERE status IN ('deployed', 'removed') AND account_id IS NOT NULL
                   GROUP BY account_id
               ) GROUP BY account_id"""
        ).fetchall()]

        account_post_ownership = [dict(r) for r in self.conn.execute(
            """SELECT owner_account as account_id, COUNT(*) as cnt FROM posts
               WHERE owner_account IS NOT NULL AND owner_account != ''
               GROUP BY owner_account"""
        ).fetchall()]

        account_sub_spread = [dict(r) for r in self.conn.execute(
            """SELECT account_id, COUNT(DISTINCT sub) as cnt FROM (
                   SELECT c.account_id, p.subreddit_id as sub FROM comments c
                   JOIN posts p ON c.post_id = p.id
                   WHERE c.account_id IS NOT NULL AND c.status IN ('assigned','informed','deployed','paid')
                   UNION ALL
                   SELECT sc.account_id, sp.subreddit as sub FROM search_comments sc
                   JOIN search_posts sp ON sc.search_post_id = sp.id
                   WHERE sc.account_id IS NOT NULL AND sc.status IN ('assigned','informed','deployed','paid')
               ) GROUP BY account_id"""
        ).fetchall()]

        # Subreddit deployed counts — scoped to single target subreddit
        subreddit_deployed_counts = [dict(r) for r in self.conn.execute(
            """SELECT account_id, SUM(cnt) as cnt FROM (
                   SELECT c.account_id, COUNT(*) as cnt FROM comments c
                   JOIN posts p ON c.post_id = p.id
                   JOIN subreddits s ON p.subreddit_id = s.id
                   WHERE s.name = ? AND c.account_id IS NOT NULL
                     AND c.status = 'deployed'
                   GROUP BY c.account_id
                   UNION ALL
                   SELECT sc.account_id, COUNT(*) as cnt FROM search_comments sc
                   JOIN search_posts sp ON sc.search_post_id = sp.id
                   WHERE sp.subreddit = ? AND sc.account_id IS NOT NULL
                     AND sc.status = 'deployed'
                   GROUP BY sc.account_id
               ) GROUP BY account_id""",
            (sub_name, sub_name)
        ).fetchall()]

        return {
            "target_comment": target,
            "draft_comments": [target],
            "all_accounts": all_accounts,
            "subreddit_day_assignments": subreddit_day_assignments,
            "account_pending_counts": account_pending_counts,
            "account_brand_mentions": account_brand_mentions,
            "account_total_mentions": account_total_mentions,
            "subreddit_veterans": set(),
            "account_deployed_counts": account_deployed_counts,
            "subreddit_deployed_counts": subreddit_deployed_counts,
            "account_post_ownership": account_post_ownership,
            "account_sub_spread": account_sub_spread,
        }

    def get_accounts_for_filters(self, subreddit_id=None, brand_id=None, post_id=None):
        rows = self.conn.execute(
            """SELECT DISTINCT c.account_id as username
               FROM comments c
               JOIN posts p ON c.post_id = p.id
               WHERE c.account_id IS NOT NULL
                 AND (? IS NULL OR p.subreddit_id = ?)
                 AND (? IS NULL OR c.brand_id = ?)
                 AND (? IS NULL OR c.post_id = ?)""",
            (subreddit_id, subreddit_id, brand_id, brand_id, post_id, post_id)
        ).fetchall()
        return [r["username"] for r in rows]

    def get_comments_for_account(self, username):
        """Get all comments assigned to a specific account."""
        rows = self.conn.execute(
            """SELECT c.id, c.body, c.status, c.is_reply, c.mentions_brand,
                      c.comment_type, c.deployed_at, c.reddit_comment_url,
                      p.title as post_title,
                      pu.reddit_url as post_reddit_url,
                      s.name as subreddit_name,
                      b.name as brand_name
               FROM comments c
               LEFT JOIN posts p ON c.post_id = p.id
               LEFT JOIN post_urls pu ON pu.post_id = p.id
               LEFT JOIN subreddits s ON p.subreddit_id = s.id
               LEFT JOIN brands b ON c.brand_id = b.id
               WHERE c.account_id = ?
               ORDER BY c.created_at DESC""",
            (username,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_accounts_with_search_assignment_counts(self):
        rows = self.conn.execute(
            """SELECT a.*,
                      COUNT(sc.id) as total_assigned,
                      SUM(CASE WHEN sc.status IN ('assigned','informed') THEN 1 ELSE 0 END) as pending,
                      SUM(CASE WHEN sc.status = 'deployed' THEN 1 ELSE 0 END) as deployed,
                      SUM(CASE WHEN sc.status = 'deleted' THEN 1 ELSE 0 END) as deleted,
                      SUM(CASE WHEN sc.status = 'paid' THEN 1 ELSE 0 END) as paid,
                      SUM(CASE WHEN sc.status = 'deployed' AND sc.deployed_at < datetime('now', '-4 days') AND sc.deleted_at IS NULL THEN 1 ELSE 0 END) as due_payment
               FROM accounts a
               LEFT JOIN search_comments sc ON sc.account_id = a.username
               GROUP BY a.id
               ORDER BY a.username"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_search_comments_for_account(self, username):
        rows = self.conn.execute(
            """SELECT sc.id, sc.body, sc.status, sc.is_reply, sc.mentions_brand,
                      sc.deployed_at, sc.reddit_comment_url, sc.reply_to_url,
                      sc.paid_at, sc.deleted_at,
                      sp.title as post_title, sp.reddit_url as post_reddit_url,
                      sp.subreddit as subreddit_name,
                      b.name as brand_name
               FROM search_comments sc
               LEFT JOIN search_posts sp ON sc.search_post_id = sp.id
               LEFT JOIN brands b ON sc.brand_id = b.id
               WHERE sc.account_id = ?
               ORDER BY sc.created_at DESC""",
            (username,)
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_comment_paid(self, comment_id):
        row = self.conn.execute("SELECT status FROM comments WHERE id = ?", (comment_id,)).fetchone()
        old_status = row["status"] if row else None
        self.conn.execute(
            "UPDATE comments SET status = 'paid', prev_status = ?, paid_at = datetime('now') WHERE id = ?",
            (old_status, comment_id)
        )
        self.conn.commit()

    def unmark_comment_paid(self, comment_id):
        """Revert a 'paid' comment back to 'deployed' (clears paid_at).

        Reliable regardless of prev_status — a paid comment is always a
        deployed comment that was flagged paid, so 'deployed' is the
        correct target. Returns 'deployed' on success, None if the row
        wasn't in paid state.
        """
        row = self.conn.execute("SELECT status FROM comments WHERE id = ?", (comment_id,)).fetchone()
        if not row or row["status"] != "paid":
            return None
        self.conn.execute(
            "UPDATE comments SET status = 'deployed', paid_at = NULL, prev_status = NULL WHERE id = ?",
            (comment_id,)
        )
        self.conn.commit()
        return "deployed"

    def mark_post_paid(self, post_id):
        self.conn.execute(
            "UPDATE posts SET status = 'paid', paid_at = datetime('now') WHERE id = ?",
            (post_id,)
        )
        self.conn.commit()

    def mark_search_comment_paid(self, comment_id):
        row = self.conn.execute("SELECT status FROM search_comments WHERE id = ?", (comment_id,)).fetchone()
        old_status = row["status"] if row else None
        self.conn.execute(
            "UPDATE search_comments SET status = 'paid', prev_status = ?, paid_at = datetime('now') WHERE id = ?",
            (old_status, comment_id)
        )
        self.conn.commit()

    def bulk_mark_paid(self, brand_id=None, subreddit_id=None, account_id=None, source=None, date=None):
        """Mark all deployed comments matching filters as paid. Returns count of updated rows."""
        total = 0

        # Build WHERE for regular comments
        if source != 'search_comment':
            w1, p1 = ["status = 'deployed'"], []
            if brand_id:
                w1.append("brand_id = ?"); p1.append(brand_id)
            if account_id:
                w1.append("account_id = ?"); p1.append(account_id)
            if subreddit_id:
                w1.append("post_id IN (SELECT id FROM posts WHERE subreddit_id = ?)"); p1.append(subreddit_id)
            if date:
                w1.append("DATE(COALESCE(deployed_at, created_at)) = ?"); p1.append(date)
            where1 = " AND ".join(w1)
            cur = self.conn.execute(
                f"UPDATE comments SET status = 'paid', paid_at = datetime('now') WHERE {where1}", p1
            )
            total += cur.rowcount

        # Build WHERE for search comments
        if source != 'comment':
            w2, p2 = ["status = 'deployed'"], []
            if brand_id:
                w2.append("brand_id = ?"); p2.append(brand_id)
            if account_id:
                w2.append("account_id = ?"); p2.append(account_id)
            if subreddit_id:
                row = self.conn.execute("SELECT name FROM subreddits WHERE id = ?", (subreddit_id,)).fetchone()
                if row:
                    w2.append("search_post_id IN (SELECT id FROM search_posts WHERE LOWER(subreddit) = LOWER(?))")
                    p2.append(row["name"])
            if date:
                w2.append("DATE(COALESCE(deployed_at, created_at)) = ?"); p2.append(date)
            where2 = " AND ".join(w2)
            cur = self.conn.execute(
                f"UPDATE search_comments SET status = 'paid', paid_at = datetime('now') WHERE {where2}", p2
            )
            total += cur.rowcount

        self.conn.commit()
        return total

    def get_due_payments(self):
        rows = self.conn.execute(
            """SELECT 'comment' as source, c.id, c.body, c.account_id, c.deployed_at,
                      p.title as post_title, b.name as brand_name
               FROM comments c
               LEFT JOIN posts p ON c.post_id = p.id
               LEFT JOIN brands b ON c.brand_id = b.id
               WHERE c.status = 'deployed'
                 AND c.deployed_at < datetime('now', '-4 days')
                 AND c.paid_at IS NULL
                 AND c.deleted_at IS NULL
               UNION ALL
               SELECT 'search_comment' as source, sc.id, sc.body, sc.account_id, sc.deployed_at,
                      sp.title as post_title, b.name as brand_name
               FROM search_comments sc
               LEFT JOIN search_posts sp ON sc.search_post_id = sp.id
               LEFT JOIN brands b ON sc.brand_id = b.id
               WHERE sc.status = 'deployed'
                 AND sc.deployed_at < datetime('now', '-4 days')
                 AND sc.paid_at IS NULL
                 AND sc.deleted_at IS NULL
               ORDER BY deployed_at ASC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_payment_data(self, subreddit_id=None, brand_id=None, account_id=None,
                         paid_filter=None, limit=200, offset=0):
        """Get all deployed comments and posts with payment status.

        Args:
            subreddit_id: Filter by subreddit
            brand_id: Filter by brand
            account_id: Filter by account
            paid_filter: 'paid', 'unpaid', or None (all)
            limit: Max items to return
            offset: Pagination offset

        Returns:
            dict with 'items' list, 'summary' stats, and 'total' count
        """
        # --- Summary stats (always unfiltered by paid_filter for context) ---
        sum_where = []
        sum_params = []
        if subreddit_id:
            sum_where.append("p.subreddit_id = ?")
            sum_params.append(subreddit_id)
        if brand_id:
            sum_where.append("c.brand_id = ?")
            sum_params.append(brand_id)
        if account_id:
            sum_where.append("c.account_id = ?")
            sum_params.append(account_id)
        sw = ("AND " + " AND ".join(sum_where)) if sum_where else ""

        summary = dict(self.conn.execute(f"""
            SELECT
                COUNT(CASE WHEN c.status = 'paid' THEN 1 END) as paid_comments,
                COUNT(CASE WHEN c.status = 'deployed' THEN 1 END) as unpaid_comments
            FROM comments c
            JOIN posts p ON c.post_id = p.id
            WHERE c.status = 'deployed' {sw}
        """, sum_params).fetchone())

        # Posts summary
        pw = []
        pp = []
        if subreddit_id:
            pw.append("p.subreddit_id = ?")
            pp.append(subreddit_id)
        if brand_id:
            pw.append("EXISTS (SELECT 1 FROM post_brands pb WHERE pb.post_id = p.id AND pb.brand_id = ?)")
            pp.append(brand_id)
        pw_sql = ("AND " + " AND ".join(pw)) if pw else ""

        post_summary = dict(self.conn.execute(f"""
            SELECT
                COUNT(CASE WHEN p.status = 'paid' THEN 1 END) as paid_posts,
                COUNT(CASE WHEN p.status = 'published' THEN 1 END) as unpaid_posts
            FROM posts p
            WHERE p.status IN ('published', 'paid') {pw_sql}
        """, pp).fetchone())
        summary.update(post_summary)

        # Search comments summary
        sc_where = []
        sc_params = []
        if brand_id:
            sc_where.append("sc.brand_id = ?")
            sc_params.append(brand_id)
        if account_id:
            sc_where.append("sc.account_id = ?")
            sc_params.append(account_id)
        scw = ("AND " + " AND ".join(sc_where)) if sc_where else ""

        sc_summary = dict(self.conn.execute(f"""
            SELECT
                COUNT(CASE WHEN sc.status = 'paid' THEN 1 END) as paid_search_comments,
                COUNT(CASE WHEN sc.status = 'deployed' THEN 1 END) as unpaid_search_comments
            FROM search_comments sc
            WHERE sc.status = 'deployed' {scw}
        """, sc_params).fetchone())
        summary.update(sc_summary)

        # --- Items list (comments + posts + search_comments) with UNION ---
        # Build WHERE for comments
        c_where = ["c.status IN ('deployed', 'paid')"]
        if paid_filter == 'paid':
            c_where.append("c.status = 'paid'")
        elif paid_filter == 'unpaid':
            c_where.append("c.status = 'deployed'")
        c_params = []
        if subreddit_id:
            c_where.append("p.subreddit_id = ?")
            c_params.append(subreddit_id)
        if brand_id:
            c_where.append("c.brand_id = ?")
            c_params.append(brand_id)
        if account_id:
            c_where.append("c.account_id = ?")
            c_params.append(account_id)
        c_where_sql = " AND ".join(c_where)

        # Build WHERE for posts
        p_where = ["p.status IN ('published', 'paid')"]
        if paid_filter == 'paid':
            p_where.append("p.status = 'paid'")
        elif paid_filter == 'unpaid':
            p_where.append("p.status = 'published'")
        p_params = []
        if subreddit_id:
            p_where.append("p.subreddit_id = ?")
            p_params.append(subreddit_id)
        if brand_id:
            p_where.append("EXISTS (SELECT 1 FROM post_brands pb WHERE pb.post_id = p.id AND pb.brand_id = ?)")
            p_params.append(brand_id)
        if account_id:
            p_where.append("p.owner_account = ?")
            p_params.append(account_id)
        p_where_sql = " AND ".join(p_where)

        # Build WHERE for search comments
        sc_where2 = ["sc.status IN ('deployed', 'paid')"]
        if paid_filter == 'paid':
            sc_where2.append("sc.status = 'paid'")
        elif paid_filter == 'unpaid':
            sc_where2.append("sc.status = 'deployed'")
        sc_params2 = []
        if brand_id:
            sc_where2.append("sc.brand_id = ?")
            sc_params2.append(brand_id)
        if account_id:
            sc_where2.append("sc.account_id = ?")
            sc_params2.append(account_id)
        sc_where_sql = " AND ".join(sc_where2)

        query = f"""
            SELECT * FROM (
                SELECT 'comment' as type, c.id, c.body, c.account_id, c.deployed_at,
                       c.paid_at, c.mentions_brand, p.title as post_title,
                       b.name as brand_name, s.name as subreddit_name
                FROM comments c
                JOIN posts p ON c.post_id = p.id
                LEFT JOIN brands b ON c.brand_id = b.id
                LEFT JOIN subreddits s ON p.subreddit_id = s.id
                WHERE {c_where_sql}

                UNION ALL

                SELECT 'post' as type, p.id, p.title as body, p.owner_account as account_id,
                       p.deployed_at, p.paid_at, 0 as mentions_brand, p.title as post_title,
                       GROUP_CONCAT(DISTINCT b2.name) as brand_name, s.name as subreddit_name
                FROM posts p
                LEFT JOIN post_brands pb ON pb.post_id = p.id
                LEFT JOIN brands b2 ON b2.id = pb.brand_id
                LEFT JOIN subreddits s ON p.subreddit_id = s.id
                WHERE {p_where_sql}
                GROUP BY p.id

                UNION ALL

                SELECT 'search_comment' as type, sc.id, sc.body, sc.account_id, sc.deployed_at,
                       sc.paid_at, sc.mentions_brand, sp.title as post_title,
                       b.name as brand_name, sp.subreddit as subreddit_name
                FROM search_comments sc
                LEFT JOIN search_posts sp ON sc.search_post_id = sp.id
                LEFT JOIN brands b ON sc.brand_id = b.id
                WHERE {sc_where_sql}
            ) combined
            ORDER BY paid_at IS NULL DESC, deployed_at DESC
            LIMIT ? OFFSET ?
        """
        all_params = c_params + p_params + sc_params2 + [limit, offset]
        rows = self.conn.execute(query, all_params).fetchall()
        items = [dict(r) for r in rows]

        # Total count for pagination
        count_query = f"""
            SELECT (
                SELECT COUNT(*) FROM comments c JOIN posts p ON c.post_id = p.id
                WHERE {c_where_sql}
            ) + (
                SELECT COUNT(*) FROM posts p
                WHERE {p_where_sql}
            ) + (
                SELECT COUNT(*) FROM search_comments sc
                WHERE {sc_where_sql}
            ) as total
        """
        count_params = c_params + p_params + sc_params2
        total = self.conn.execute(count_query, count_params).fetchone()[0]

        return {"items": items, "summary": summary, "total": total}

    # --- Calendar Events ---

    def get_calendar_events(self, date_from=None, date_to=None, brand_id=None,
                            subreddit_id=None, account_id=None, status=None,
                            event_type=None, ref=None):
        """Get unified calendar events: published posts + assigned/deployed comments."""
        queries = []
        all_params = []

        is_paid = status == 'paid'

        # --- Query 1: Published Posts ---
        has_posts = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='post_urls'").fetchone()
        if has_posts and (not event_type or event_type == 'post'):
            if is_paid:
                # For paid filter, show posts with paid_at set
                q1 = """SELECT 'post' as event_type, p.id as event_id,
                               COALESCE(p.paid_at, pu.added_at) as event_date,
                               p.title as title, p.body as body,
                               b.name as brand_name, b.id as brand_id,
                               s.name as subreddit_name, s.id as subreddit_id,
                               p.owner_account as account_id,
                               p.status as status,
                               pu.reddit_url as reddit_url,
                               NULL as reddit_comment_url,
                               0 as is_reply, 0 as mentions_brand,
                               NULL as reply_to_url,
                               p.paid_at as paid_at
                        FROM posts p
                        JOIN post_urls pu ON pu.post_id = p.id
                        JOIN subreddits s ON p.subreddit_id = s.id
                        LEFT JOIN brands b ON p.brand_id = b.id
                        WHERE p.status = 'paid'"""
            else:
                q1 = """SELECT 'post' as event_type, p.id as event_id,
                               pu.added_at as event_date,
                               p.title as title, p.body as body,
                               b.name as brand_name, b.id as brand_id,
                               s.name as subreddit_name, s.id as subreddit_id,
                               p.owner_account as account_id,
                               p.status as status,
                               pu.reddit_url as reddit_url,
                               NULL as reddit_comment_url,
                               0 as is_reply, 0 as mentions_brand,
                               NULL as reply_to_url,
                               p.paid_at as paid_at
                        FROM posts p
                        JOIN post_urls pu ON pu.post_id = p.id
                        JOIN subreddits s ON p.subreddit_id = s.id
                        LEFT JOIN brands b ON p.brand_id = b.id
                        WHERE pu.added_at IS NOT NULL"""
            p1 = []
            if date_from:
                if is_paid:
                    q1 += " AND COALESCE(p.paid_at, pu.added_at) >= ?"
                else:
                    q1 += " AND pu.added_at >= ?"
                p1.append(date_from)
            if date_to:
                if is_paid:
                    q1 += " AND COALESCE(p.paid_at, pu.added_at) <= ?"
                else:
                    q1 += " AND pu.added_at <= ?"
                p1.append(date_to + " 23:59:59")
            if brand_id:
                q1 += " AND b.id = ?"
                p1.append(brand_id)
            if subreddit_id:
                q1 += " AND s.id = ?"
                p1.append(subreddit_id)
            if account_id:
                q1 += " AND p.owner_account = ?"
                p1.append(account_id)
            if ref:
                q1 += " AND p.owner_account IN (SELECT username FROM accounts WHERE reference LIKE '%' || ? || '%')"
                p1.append(ref)
            if status and status not in ('published', 'paid'):
                pass  # skip posts if filtering for non-published/non-paid status
            else:
                queries.append(q1)
                all_params.extend(p1)

        # --- Query 2: Comments (assigned/deployed) ---
        has_comments = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='comments'").fetchone()
        if has_comments and (not event_type or event_type in ('comment', 'search_comment')):
            if not event_type or event_type != 'search_comment':
                date_expr = "CASE WHEN c.status = 'paid' THEN COALESCE(c.paid_at, c.deployed_at, c.created_at) ELSE COALESCE(c.deployed_at, c.created_at) END"
                where_clause = "WHERE c.status IN ('assigned', 'informed', 'deployed', 'paid')"
                q2 = f"""SELECT 'comment' as event_type, c.id as event_id,
                               {date_expr} as event_date,
                               p.title as title, c.body as body,
                               b.name as brand_name, b.id as brand_id,
                               s.name as subreddit_name, s.id as subreddit_id,
                               c.account_id as account_id,
                               c.status as status,
                               pu.reddit_url as reddit_url,
                               c.reddit_comment_url as reddit_comment_url,
                               c.is_reply as is_reply, c.mentions_brand as mentions_brand,
                               NULL as reply_to_url,
                               c.paid_at as paid_at
                        FROM comments c
                        JOIN posts p ON c.post_id = p.id
                        JOIN subreddits s ON p.subreddit_id = s.id
                        LEFT JOIN brands b ON c.brand_id = b.id
                        LEFT JOIN post_urls pu ON pu.post_id = p.id
                        {where_clause}"""
                p2 = []
                if date_from:
                    q2 += f" AND {date_expr} >= ?"
                    p2.append(date_from)
                if date_to:
                    q2 += f" AND {date_expr} <= ?"
                    p2.append(date_to + " 23:59:59")
                if brand_id:
                    q2 += " AND b.id = ?"
                    p2.append(brand_id)
                if subreddit_id:
                    q2 += " AND s.id = ?"
                    p2.append(subreddit_id)
                if account_id:
                    q2 += " AND c.account_id = ?"
                    p2.append(account_id)
                if ref:
                    q2 += " AND c.account_id IN (SELECT username FROM accounts WHERE reference LIKE '%' || ? || '%')"
                    p2.append(ref)
                if status:
                    q2 += " AND c.status = ?"
                    p2.append(status)
                queries.append(q2)
                all_params.extend(p2)

        # --- Query 3: Search Comments (assigned/deployed) ---
        has_sc = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='search_comments'").fetchone()
        if has_sc and (not event_type or event_type in ('comment', 'search_comment')):
            if not event_type or event_type != 'comment':
                date_expr3 = "CASE WHEN sc.status = 'paid' THEN COALESCE(sc.paid_at, sc.deployed_at, sc.created_at) ELSE COALESCE(sc.deployed_at, sc.created_at) END"
                where_clause3 = "WHERE sc.status IN ('assigned', 'informed', 'deployed', 'paid')"
                q3 = f"""SELECT 'search_comment' as event_type, sc.id as event_id,
                               {date_expr3} as event_date,
                               sp.title as title, sc.body as body,
                               b.name as brand_name, b.id as brand_id,
                               sp.subreddit as subreddit_name, NULL as subreddit_id,
                               sc.account_id as account_id,
                               sc.status as status,
                               sp.reddit_url as reddit_url,
                               sc.reddit_comment_url as reddit_comment_url,
                               sc.is_reply as is_reply, sc.mentions_brand as mentions_brand,
                               sc.reply_to_url as reply_to_url,
                               sc.paid_at as paid_at
                        FROM search_comments sc
                        JOIN search_posts sp ON sc.search_post_id = sp.id
                        LEFT JOIN brands b ON sc.brand_id = b.id
                        {where_clause3}"""
                p3 = []
                if date_from:
                    q3 += f" AND {date_expr3} >= ?"
                    p3.append(date_from)
                if date_to:
                    q3 += f" AND {date_expr3} <= ?"
                    p3.append(date_to + " 23:59:59")
                if brand_id:
                    q3 += " AND b.id = ?"
                    p3.append(brand_id)
                if subreddit_id:
                    q3 += " AND sp.subreddit = (SELECT name FROM subreddits WHERE id = ?)"
                    p3.append(subreddit_id)
                if account_id:
                    q3 += " AND sc.account_id = ?"
                    p3.append(account_id)
                if ref:
                    q3 += " AND sc.account_id IN (SELECT username FROM accounts WHERE reference LIKE '%' || ? || '%')"
                    p3.append(ref)
                if status:
                    q3 += " AND sc.status = ?"
                    p3.append(status)
                queries.append(q3)
                all_params.extend(p3)

        if not queries:
            return []

        full_query = " UNION ALL ".join(queries) + " ORDER BY event_date DESC"
        rows = self.conn.execute(full_query, all_params).fetchall()
        return [dict(r) for r in rows]

    def get_calendar_account_summary(self, date, brand_id=None, subreddit_id=None, ref=None):
        """Get per-account counts of assigned/deployed/paid for a given date."""
        results = {}
        day_start = date
        day_end = date + " 23:59:59"

        def _ensure(acct):
            if acct not in results:
                results[acct] = {'account_id': acct,
                                 'reg_brand_assigned': 0, 'reg_nonbrand_assigned': 0,
                                 'reg_brand_deployed': 0, 'reg_nonbrand_deployed': 0,
                                 'reg_brand_paid': 0, 'reg_nonbrand_paid': 0,
                                 'ls_assigned': 0, 'ls_deployed': 0, 'ls_paid': 0,
                                 'posts_deployed': 0, 'posts_paid': 0}

        # --- Regular comments (non-paid): use deployed_at/created_at range ---
        q1a = """SELECT c.account_id,
                       SUM(CASE WHEN c.status = 'assigned' AND c.mentions_brand = 1 THEN 1 ELSE 0 END) as reg_brand_assigned,
                       SUM(CASE WHEN c.status = 'assigned' AND (c.mentions_brand = 0 OR c.mentions_brand IS NULL) THEN 1 ELSE 0 END) as reg_nonbrand_assigned,
                       SUM(CASE WHEN c.status = 'deployed' AND c.mentions_brand = 1 THEN 1 ELSE 0 END) as reg_brand_deployed,
                       SUM(CASE WHEN c.status = 'deployed' AND (c.mentions_brand = 0 OR c.mentions_brand IS NULL) THEN 1 ELSE 0 END) as reg_nonbrand_deployed
                FROM comments c
                JOIN posts p ON c.post_id = p.id
                LEFT JOIN brands b ON c.brand_id = b.id
                WHERE c.status IN ('assigned', 'informed', 'deployed')
                  AND c.account_id IS NOT NULL
                  AND COALESCE(c.deployed_at, c.created_at) >= ? AND COALESCE(c.deployed_at, c.created_at) <= ?"""
        p1a = [day_start, day_end]
        if brand_id:
            q1a += " AND b.id = ?"; p1a.append(brand_id)
        if subreddit_id:
            q1a += " AND p.subreddit_id = ?"; p1a.append(subreddit_id)
        if ref:
            q1a += " AND c.account_id IN (SELECT username FROM accounts WHERE reference LIKE '%' || ? || '%')"; p1a.append(ref)
        q1a += " GROUP BY c.account_id"
        for row in self.conn.execute(q1a, p1a).fetchall():
            r = dict(row)
            acct = r['account_id']; _ensure(acct)
            for k in ('reg_brand_assigned', 'reg_nonbrand_assigned', 'reg_brand_deployed', 'reg_nonbrand_deployed'):
                results[acct][k] = r[k]

        # --- Regular comments (paid): use paid_at range ---
        q1b = """SELECT c.account_id,
                       SUM(CASE WHEN c.mentions_brand = 1 THEN 1 ELSE 0 END) as reg_brand_paid,
                       SUM(CASE WHEN c.mentions_brand = 0 OR c.mentions_brand IS NULL THEN 1 ELSE 0 END) as reg_nonbrand_paid
                FROM comments c
                JOIN posts p ON c.post_id = p.id
                LEFT JOIN brands b ON c.brand_id = b.id
                WHERE c.status = 'paid'
                  AND c.account_id IS NOT NULL
                  AND COALESCE(c.paid_at, c.deployed_at, c.created_at) >= ? AND COALESCE(c.paid_at, c.deployed_at, c.created_at) <= ?"""
        p1b = [day_start, day_end]
        if brand_id:
            q1b += " AND b.id = ?"; p1b.append(brand_id)
        if subreddit_id:
            q1b += " AND p.subreddit_id = ?"; p1b.append(subreddit_id)
        if ref:
            q1b += " AND c.account_id IN (SELECT username FROM accounts WHERE reference LIKE '%' || ? || '%')"; p1b.append(ref)
        q1b += " GROUP BY c.account_id"
        for row in self.conn.execute(q1b, p1b).fetchall():
            r = dict(row)
            acct = r['account_id']; _ensure(acct)
            results[acct]['reg_brand_paid'] = r['reg_brand_paid']
            results[acct]['reg_nonbrand_paid'] = r['reg_nonbrand_paid']

        # --- Search comments (non-paid) ---
        q2a = """SELECT sc.account_id,
                       SUM(CASE WHEN sc.status = 'assigned' THEN 1 ELSE 0 END) as ls_assigned,
                       SUM(CASE WHEN sc.status = 'deployed' THEN 1 ELSE 0 END) as ls_deployed
                FROM search_comments sc
                JOIN search_posts sp ON sc.search_post_id = sp.id
                LEFT JOIN brands b ON sc.brand_id = b.id
                WHERE sc.status IN ('assigned', 'informed', 'deployed')
                  AND sc.account_id IS NOT NULL
                  AND COALESCE(sc.deployed_at, sc.created_at) >= ? AND COALESCE(sc.deployed_at, sc.created_at) <= ?"""
        p2a = [day_start, day_end]
        if brand_id:
            q2a += " AND b.id = ?"; p2a.append(brand_id)
        if subreddit_id:
            q2a += " AND sp.subreddit = (SELECT name FROM subreddits WHERE id = ?)"; p2a.append(subreddit_id)
        if ref:
            q2a += " AND sc.account_id IN (SELECT username FROM accounts WHERE reference LIKE '%' || ? || '%')"; p2a.append(ref)
        q2a += " GROUP BY sc.account_id"
        for row in self.conn.execute(q2a, p2a).fetchall():
            r = dict(row)
            acct = r['account_id']; _ensure(acct)
            results[acct]['ls_assigned'] = r['ls_assigned']
            results[acct]['ls_deployed'] = r['ls_deployed']

        # --- Search comments (paid) ---
        q2b = """SELECT sc.account_id, COUNT(*) as ls_paid
                FROM search_comments sc
                JOIN search_posts sp ON sc.search_post_id = sp.id
                LEFT JOIN brands b ON sc.brand_id = b.id
                WHERE sc.status = 'paid'
                  AND sc.account_id IS NOT NULL
                  AND COALESCE(sc.paid_at, sc.deployed_at, sc.created_at) >= ? AND COALESCE(sc.paid_at, sc.deployed_at, sc.created_at) <= ?"""
        p2b = [day_start, day_end]
        if brand_id:
            q2b += " AND b.id = ?"; p2b.append(brand_id)
        if subreddit_id:
            q2b += " AND sp.subreddit = (SELECT name FROM subreddits WHERE id = ?)"; p2b.append(subreddit_id)
        if ref:
            q2b += " AND sc.account_id IN (SELECT username FROM accounts WHERE reference LIKE '%' || ? || '%')"; p2b.append(ref)
        q2b += " GROUP BY sc.account_id"
        for row in self.conn.execute(q2b, p2b).fetchall():
            r = dict(row)
            acct = r['account_id']; _ensure(acct)
            results[acct]['ls_paid'] = r['ls_paid']

        # --- Posts (deployed): by post_urls.added_at ---
        q3a = """SELECT p.owner_account as account_id, COUNT(*) as posts_deployed
                FROM posts p
                JOIN post_urls pu ON pu.post_id = p.id
                JOIN subreddits s ON p.subreddit_id = s.id
                LEFT JOIN brands b ON p.brand_id = b.id
                WHERE p.owner_account IS NOT NULL AND p.owner_account != ''
                  AND p.status = 'published'
                  AND pu.added_at >= ? AND pu.added_at <= ?"""
        p3a = [day_start, day_end]
        if brand_id:
            q3a += " AND b.id = ?"; p3a.append(brand_id)
        if subreddit_id:
            q3a += " AND s.id = ?"; p3a.append(subreddit_id)
        if ref:
            q3a += " AND p.owner_account IN (SELECT username FROM accounts WHERE reference LIKE '%' || ? || '%')"; p3a.append(ref)
        q3a += " GROUP BY p.owner_account"
        for row in self.conn.execute(q3a, p3a).fetchall():
            r = dict(row)
            acct = r['account_id']; _ensure(acct)
            results[acct]['posts_deployed'] = r['posts_deployed']

        # --- Posts (paid): by paid_at ---
        q3b = """SELECT p.owner_account as account_id, COUNT(*) as posts_paid
                FROM posts p
                JOIN post_urls pu ON pu.post_id = p.id
                JOIN subreddits s ON p.subreddit_id = s.id
                LEFT JOIN brands b ON p.brand_id = b.id
                WHERE p.owner_account IS NOT NULL AND p.owner_account != ''
                  AND p.status = 'paid'
                  AND COALESCE(p.paid_at, pu.added_at) >= ? AND COALESCE(p.paid_at, pu.added_at) <= ?"""
        p3b = [day_start, day_end]
        if brand_id:
            q3b += " AND b.id = ?"; p3b.append(brand_id)
        if subreddit_id:
            q3b += " AND s.id = ?"; p3b.append(subreddit_id)
        if ref:
            q3b += " AND p.owner_account IN (SELECT username FROM accounts WHERE reference LIKE '%' || ? || '%')"; p3b.append(ref)
        q3b += " GROUP BY p.owner_account"
        for row in self.conn.execute(q3b, p3b).fetchall():
            r = dict(row)
            acct = r['account_id']; _ensure(acct)
            results[acct]['posts_paid'] = r['posts_paid']

        # Add reference from accounts table
        acct_names = list(results.keys())
        if acct_names:
            placeholders = ','.join('?' * len(acct_names))
            refs = self.conn.execute(
                f"SELECT username, reference FROM accounts WHERE username IN ({placeholders})",
                acct_names
            ).fetchall()
            ref_map = {r['username']: r['reference'] for r in refs}
            for acct in results.values():
                acct['reference'] = ref_map.get(acct['account_id'], '')

        return list(results.values())

    # --- Search Posts (Live Search) ---

    def save_search_post(self, data):
        """Insert a search post, or update brand_id if URL already exists.

        Returns (id, is_new). When the reddit_url already exists, the post's
        brand_id is updated to data['brand_id'] (if provided) and the
        existing row id is returned with is_new=False. This lets callers
        re-point a saved post at a different brand in a single step.
        """
        try:
            cur = self.conn.execute(
                """INSERT INTO search_posts
                   (reddit_url, title, subreddit, score, num_comments, author, post_date, body_preview, brand_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (data["reddit_url"], data["title"], data["subreddit"],
                 data.get("score", 0), data.get("num_comments", 0),
                 data.get("author", ""), data.get("post_date", ""),
                 data.get("body_preview", "")[:500], data.get("brand_id"))
            )
            self.conn.commit()
            return (cur.lastrowid, True)
        except sqlite3.IntegrityError:
            row = self.conn.execute(
                "SELECT id FROM search_posts WHERE reddit_url = ?",
                (data["reddit_url"],)
            ).fetchone()
            if not row:
                return (None, False)
            if data.get("brand_id"):
                self.conn.execute(
                    "UPDATE search_posts SET brand_id = ? WHERE id = ?",
                    (data["brand_id"], row["id"])
                )
                self.conn.commit()
            return (row["id"], False)

    def list_search_posts(self, brand_id=None, status=None):
        q = """SELECT sp.*, b.name as brand_name,
                      (SELECT COUNT(*) FROM search_comments sc WHERE sc.search_post_id = sp.id AND sc.status != 'deleted') as comment_count
               FROM search_posts sp
               LEFT JOIN brands b ON sp.brand_id = b.id
               WHERE 1=1"""
        params = []
        if brand_id:
            q += " AND sp.brand_id = ?"
            params.append(brand_id)
        if status:
            q += " AND sp.status = ?"
            params.append(status)
        q += " ORDER BY sp.created_at DESC"
        rows = self.conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def get_search_post(self, post_id):
        row = self.conn.execute("SELECT * FROM search_posts WHERE id = ?", (post_id,)).fetchone()
        return dict(row) if row else None

    def update_search_post_status(self, post_id, status):
        self.conn.execute("UPDATE search_posts SET status = ? WHERE id = ?", (status, post_id))
        self.conn.commit()

    def search_post_has_comments_for_brand(self, search_post_id, brand_id, hq=False):
        """True if this search post already has a non-deleted comment for `brand_id` of the
        given kind (hq vs regular). Mode-aware dedup for the batch generators — lets the
        server skip already-covered posts WITHOUT the client pulling the whole comments
        table. Single indexed lookup."""
        kind = "comment_type = 'hq'" if hq else "(comment_type IS NULL OR comment_type != 'hq')"
        row = self.conn.execute(
            f"SELECT 1 FROM search_comments WHERE search_post_id = ? AND brand_id = ? "
            f"AND status != 'deleted' AND {kind} LIMIT 1",
            (search_post_id, brand_id)
        ).fetchone()
        return bool(row)

    def delete_search_post(self, post_id):
        # Decrement the global rotation counter once for each child search_comment
        # that still has an account attached. The UPDATE below does this in a
        # single round-trip: (count of affected rows) per distinct account_id.
        rows = self.conn.execute(
            """SELECT account_id, COUNT(*) AS cnt FROM search_comments
               WHERE search_post_id = ? AND account_id IS NOT NULL
                 AND status != 'deleted'
               GROUP BY account_id""",
            (post_id,)
        ).fetchall()
        for r in rows:
            self._decrement_lifetime(r["account_id"], r["cnt"])
        self.conn.execute("DELETE FROM search_comments WHERE search_post_id = ?", (post_id,))
        self.conn.execute("DELETE FROM search_posts WHERE id = ?", (post_id,))
        self.conn.commit()

    # Statuses that count as "untouched / original" — safe to purge.
    _LS_PURGE_POST_STATUSES = ('saved', 'complete')
    _LS_PURGE_SAFE_COMMENT_STATUSES = ('draft', 'complete')

    def purge_untouched_search_posts(self, month, dry_run=False):
        """Delete Live Search posts (and their comments) that are still in their ORIGINAL
        state, created up to and including `month` ('YYYY-MM'). CONSERVATIVE gate — a post
        is purgeable ONLY when its status is saved/complete AND none of its comments has a
        status outside draft/complete (any deployed/reported/etc. comment protects it).

        dry_run=True → counts only ({posts, comments, protected_in_range}); no writes.
        Returns {posts, comments, protected_in_range}."""
        month = (month or "").strip()
        if len(month) != 7 or month[4] != '-':
            raise ValueError("month must be 'YYYY-MM'")
        post_ph = ",".join("?" * len(self._LS_PURGE_POST_STATUSES))
        safe_ph = ",".join("?" * len(self._LS_PURGE_SAFE_COMMENT_STATUSES))
        gate = (f"""
            sp.status IN ({post_ph})
            AND strftime('%Y-%m', sp.created_at) <= ?
            AND NOT EXISTS (
                SELECT 1 FROM search_comments sc
                 WHERE sc.search_post_id = sp.id
                   AND sc.status NOT IN ({safe_ph})
            )""")
        params = list(self._LS_PURGE_POST_STATUSES) + [month] + list(self._LS_PURGE_SAFE_COMMENT_STATUSES)
        ids = [r["id"] for r in self.conn.execute(
            f"SELECT sp.id FROM search_posts sp WHERE {gate}", params).fetchall()]
        # Posts in the SAME date range that are NOT purgeable (have activity) — surfaced so
        # the operator can see the gate protecting worked posts.
        total_in_range = self.conn.execute(
            "SELECT COUNT(*) FROM search_posts WHERE strftime('%Y-%m', created_at) <= ?",
            (month,)).fetchone()[0]
        protected_in_range = total_in_range - len(ids)
        if not ids:
            return {"posts": 0, "comments": 0, "protected_in_range": protected_in_range}

        def _chunks(seq, n=500):
            for i in range(0, len(seq), n):
                yield seq[i:i + n]

        comment_total = 0
        for chunk in _chunks(ids):
            ph = ",".join("?" * len(chunk))
            comment_total += self.conn.execute(
                f"SELECT COUNT(*) FROM search_comments WHERE search_post_id IN ({ph})",
                chunk).fetchone()[0]
        if dry_run:
            return {"posts": len(ids), "comments": comment_total,
                    "protected_in_range": protected_in_range}

        for chunk in _chunks(ids):
            ph = ",".join("?" * len(chunk))
            # Free rotation slots for any child comment that still had an account.
            for r in self.conn.execute(
                f"""SELECT account_id, COUNT(*) AS cnt FROM search_comments
                    WHERE search_post_id IN ({ph}) AND account_id IS NOT NULL
                      AND status != 'deleted'
                    GROUP BY account_id""", chunk).fetchall():
                self._decrement_lifetime(r["account_id"], r["cnt"])
            self.conn.execute(f"DELETE FROM search_comments WHERE search_post_id IN ({ph})", chunk)
            self.conn.execute(f"DELETE FROM search_posts WHERE id IN ({ph})", chunk)
        self.conn.commit()
        return {"posts": len(ids), "comments": comment_total,
                "protected_in_range": protected_in_range}

    # --- Search Comments (Live Search) ---

    def add_search_comment(self, search_post_id, body, *,
                           brand_id=None, persona_id=None,
                           is_reply=0, reply_to_url=None,
                           mentions_brand=0, relevance_score=None,
                           comment_type=None, parent_comment_id=None):
        """Insert a search comment.

        After `search_post_id` and `body`, every argument is KEYWORD-ONLY.
        Prevents future signature drift / positional-arg confusion when
        new optional fields are added (the table has 10 nullable columns
        and growing — positional calls are a footgun).
        """
        cur = self.conn.execute(
            """INSERT INTO search_comments
               (search_post_id, body, brand_id, persona_id, is_reply, reply_to_url,
                mentions_brand, relevance_score, comment_type, parent_comment_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (search_post_id, body, brand_id, persona_id, is_reply, reply_to_url,
             mentions_brand, relevance_score, comment_type, parent_comment_id)
        )
        self.conn.commit()
        return cur.lastrowid

    def list_search_comments(self, search_post_id=None, status=None):
        q = """SELECT sc.*, sp.title as post_title, sp.subreddit as post_subreddit,
                      sp.reddit_url as post_url, sp.post_date as post_date, b.name as brand_name
               FROM search_comments sc
               JOIN search_posts sp ON sc.search_post_id = sp.id
               LEFT JOIN brands b ON sc.brand_id = b.id
               WHERE 1=1"""
        params = []
        if search_post_id:
            q += " AND sc.search_post_id = ?"
            params.append(search_post_id)
        if status:
            q += " AND sc.status = ?"
            params.append(status)
        q += " ORDER BY sc.created_at DESC"
        rows = self.conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def assign_search_comment(self, comment_id, account_id):
        row = self.conn.execute(
            "SELECT account_id, status FROM search_comments WHERE id = ?", (comment_id,)
        ).fetchone()
        prior = row["account_id"] if row else None
        old_status = row["status"] if row else None
        self.conn.execute(
            "UPDATE search_comments SET account_id = ?, status = 'assigned', prev_status = ?, assigned_at = datetime('now'), "
            "paid_at = NULL, deleted_at = NULL, reddit_comment_url = NULL, deployed_at = NULL WHERE id = ?",
            (account_id, old_status, comment_id))
        if prior and prior != account_id:
            self._decrement_lifetime(prior)
        if account_id and account_id != prior:
            self._increment_lifetime(account_id)
        self.conn.commit()

    def unassign_search_comment(self, comment_id):
        row = self.conn.execute(
            "SELECT account_id FROM search_comments WHERE id = ?", (comment_id,)
        ).fetchone()
        prior = row["account_id"] if row else None
        self.conn.execute(
            "UPDATE search_comments SET account_id = NULL, status = 'draft' WHERE id = ?",
            (comment_id,))
        if prior:
            self._decrement_lifetime(prior)
        self.conn.commit()

    def reassign_search_comment(self, comment_id, new_account_id):
        """Change account_id on an already-assigned/informed search comment
        without resetting status, assigned_at, informed_at, or prev_status."""
        row = self.conn.execute(
            "SELECT account_id FROM search_comments WHERE id = ?", (comment_id,)
        ).fetchone()
        prior = row["account_id"] if row else None
        if prior == new_account_id:
            return
        self.conn.execute(
            "UPDATE search_comments SET account_id = ? WHERE id = ?",
            (new_account_id, comment_id))
        if prior:
            self._decrement_lifetime(prior)
        if new_account_id:
            self._increment_lifetime(new_account_id)
        self.conn.commit()

    def deploy_search_comment(self, comment_id, reddit_url, deployed_at=None,
                              posted_at=None):
        """Mark a search-comment deployed. See `deploy_comment` for the
        meaning of `deployed_at` vs `posted_at`.
        """
        if not deployed_at:
            deployed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = self.conn.execute("SELECT status FROM search_comments WHERE id = ?", (comment_id,)).fetchone()
        old_status = row["status"] if row else None
        if posted_at:
            self.conn.execute(
                """UPDATE search_comments
                   SET reddit_comment_url = ?, deployed_at = ?, posted_at = ?,
                       status = 'deployed', prev_status = ?
                   WHERE id = ?""",
                (reddit_url, deployed_at, posted_at, old_status, comment_id))
        else:
            self.conn.execute(
                """UPDATE search_comments
                   SET reddit_comment_url = ?, deployed_at = ?,
                       status = 'deployed', prev_status = ?
                   WHERE id = ?""",
                (reddit_url, deployed_at, old_status, comment_id))
        self.conn.commit()

    def undeploy_search_comment(self, comment_id):
        """Revert a deployed search comment back to assigned status."""
        self.conn.execute(
            """UPDATE search_comments SET status = 'assigned', deployed_at = NULL,
               reddit_comment_url = NULL, paid_at = NULL
               WHERE id = ? AND status = 'deployed'""",
            (comment_id,)
        )
        self.conn.commit()

    def update_search_comment_body(self, comment_id, body):
        self.conn.execute("UPDATE search_comments SET body = ? WHERE id = ?", (body, comment_id))
        self.conn.commit()

    def inform_search_comment(self, comment_id):
        self.conn.execute(
            "UPDATE search_comments SET status = 'informed', prev_status = 'assigned', informed_at = datetime('now') WHERE id = ? AND status = 'assigned'",
            (comment_id,))
        self.conn.commit()

    def delete_search_comment(self, comment_id):
        deleted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # If the comment still has an owner, free its rotation slot.
        row = self.conn.execute(
            "SELECT account_id, status FROM search_comments WHERE id = ?", (comment_id,)
        ).fetchone()
        prior = row["account_id"] if row else None
        prior_status = row["status"] if row else None
        self.conn.execute(
            "UPDATE search_comments SET status = 'deleted', deleted_at = ?, paid_at = NULL WHERE id = ?",
            (deleted_at, comment_id))
        # Only decrement if this row was counted toward lifetime (i.e. had an
        # account attached and wasn't already deleted).
        if prior and prior_status != 'deleted':
            self._decrement_lifetime(prior)
        self.conn.commit()

    def mark_search_comment_removed(self, comment_id):
        """Mark a search comment as removed/deleted on Reddit (allowed for any status).

        Manual / forced path — always sets status='removed' regardless
        of how recently the comment was published. The auto-detection
        path (Check Live) should use
        `mark_search_comment_removed_or_replace` instead so the 14-day
        'replace' window can apply.
        """
        row = self.conn.execute("SELECT status FROM search_comments WHERE id = ?", (comment_id,)).fetchone()
        old_status = row["status"] if row else None
        if old_status == 'removed':
            return
        self.conn.execute(
            "UPDATE search_comments SET status = 'removed', prev_status = ?, deleted_at = datetime('now'), paid_at = NULL WHERE id = ?",
            (old_status, comment_id))
        self.conn.commit()

    def _choose_removed_status_search(self, comment_id, days=None):
        """Search-comments variant of `_choose_removed_status`. Same
        posted_at → deployed_at fallback chain as the comments-table
        variant: if Reddit can't give us a timestamp, deployed_at is
        a fine proxy for the 14-day rule.
        """
        if days is None:
            days = self.REPLACE_WINDOW_DAYS
        row = self.conn.execute(
            "SELECT posted_at, deployed_at FROM search_comments WHERE id = ?",
            (comment_id,)
        ).fetchone()
        if not row:
            return "removed"
        anchor = row["posted_at"] or row["deployed_at"]
        if not anchor:
            return "removed"
        try:
            s = str(anchor).replace("T", " ").split(".")[0].strip()
            ts = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return "removed"
        delta = datetime.utcnow() - ts
        if delta.total_seconds() < 0:
            return "replace"
        if delta.days < days:
            return "replace"
        return "removed"

    def mark_search_comment_removed_or_replace(self, comment_id,
                                                posted_at_hint=None,
                                                days=None):
        """Auto-detection variant of `mark_search_comment_removed`.

        Mirror of `mark_comment_removed_or_replace` for the
        `search_comments` table. Writes `posted_at_hint` first when
        the row's posted_at is empty, then picks 'replace' (within
        14 days of publish) or 'removed'. Preserves prev_status and
        clears paid_at; sets deleted_at for parity with the manual
        path. Idempotent.

        Returns the chosen status ('replace' | 'removed'), or None
        if the row didn't exist.
        """
        row = self.conn.execute(
            "SELECT status, posted_at FROM search_comments WHERE id = ?",
            (comment_id,)
        ).fetchone()
        if not row:
            return None
        old_status = row["status"]
        if old_status in ("removed", "replace"):
            return old_status
        if posted_at_hint and not row["posted_at"]:
            self.conn.execute(
                "UPDATE search_comments SET posted_at = ? WHERE id = ?",
                (posted_at_hint, comment_id)
            )
        new_status = self._choose_removed_status_search(comment_id, days=days)
        self.conn.execute(
            "UPDATE search_comments "
            "SET status = ?, prev_status = ?, deleted_at = datetime('now'), paid_at = NULL "
            "WHERE id = ?",
            (new_status, old_status, comment_id)
        )
        self.conn.commit()
        return new_status

    def unremove_search_comment(self, comment_id):
        """Revert a removed-or-replace search comment back to its
        previous status (fallback to deployed). Accepts both
        'removed' and 'replace' as undo-able starting states —
        'replace' is just a sub-state of removed.
        """
        row = self.conn.execute(
            "SELECT prev_status FROM search_comments WHERE id = ? "
            "AND status IN ('removed', 'replace')",
            (comment_id,)
        ).fetchone()
        if not row:
            return None
        restore_to = row["prev_status"] if row["prev_status"] else 'deployed'
        self.conn.execute(
            "UPDATE search_comments SET status = ?, prev_status = NULL "
            "WHERE id = ? AND status IN ('removed', 'replace')",
            (restore_to, comment_id))
        self.conn.commit()
        return restore_to

    def undo_search_comment_status(self, comment_id):
        """Revert a search comment to its previous status. Returns the restored status or None."""
        row = self.conn.execute(
            "SELECT prev_status FROM search_comments WHERE id = ?", (comment_id,)
        ).fetchone()
        if not row or not row["prev_status"]:
            return None
        prev = row["prev_status"]
        self.conn.execute(
            "UPDATE search_comments SET status = ?, prev_status = NULL WHERE id = ?",
            (prev, comment_id))
        self.conn.commit()
        return prev

    def archive_search_post(self, post_id):
        """Archive a search post and all its non-deployed/paid comments."""
        self.conn.execute(
            "UPDATE search_comments SET status = 'archived' WHERE search_post_id = ? AND status IN ('draft','complete','assigned','informed')",
            (post_id,))
        self.conn.execute(
            "UPDATE search_posts SET status = 'archived' WHERE id = ?",
            (post_id,))
        self.conn.commit()

    def unarchive_search_post(self, post_id):
        """Unarchive a search post and all its archived comments back to draft."""
        self.conn.execute(
            "UPDATE search_comments SET status = 'draft', account_id = NULL WHERE search_post_id = ? AND status = 'archived'",
            (post_id,))
        self.conn.execute(
            "UPDATE search_posts SET status = 'saved' WHERE id = ? AND status = 'archived'",
            (post_id,))
        self.conn.commit()

    def set_comment_live_check(self, comment_id):
        """Record that a regular comment was verified live on Reddit."""
        self.conn.execute(
            "UPDATE comments SET last_live_check = datetime('now') WHERE id = ?",
            (comment_id,))
        self.conn.commit()

    def set_search_comment_live_check(self, comment_id):
        """Record that a search comment was verified live on Reddit."""
        self.conn.execute(
            "UPDATE search_comments SET last_live_check = datetime('now') WHERE id = ?",
            (comment_id,))
        self.conn.commit()

    # ------------------------------------------------------------------
    # Subreddit scrutiny cache
    # ------------------------------------------------------------------
    def get_scrutiny(self, name, max_age_days=7):
        """Return cached scrutiny dict for a subreddit, or None if missing/stale."""
        row = self.conn.execute(
            """SELECT name, subscribers, comment_removal_rate, post_removal_rate,
                      gate_penalty, scrutiny_score, subreddit_type, computed_at,
                      CAST((julianday('now') - julianday(computed_at)) AS REAL) AS age_days
               FROM subreddit_scrutiny WHERE name = ? COLLATE NOCASE""",
            (name,)
        ).fetchone()
        if not row:
            return None
        if row["age_days"] is not None and row["age_days"] > max_age_days:
            return None
        return dict(row)

    def upsert_scrutiny(self, name, subscribers=None, comment_removal_rate=None,
                        post_removal_rate=None, gate_penalty=None,
                        scrutiny_score=None, subreddit_type=None):
        """Insert or update a subreddit scrutiny row."""
        self.conn.execute(
            """INSERT INTO subreddit_scrutiny
                 (name, subscribers, comment_removal_rate, post_removal_rate,
                  gate_penalty, scrutiny_score, subreddit_type, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(name) DO UPDATE SET
                 subscribers = excluded.subscribers,
                 comment_removal_rate = excluded.comment_removal_rate,
                 post_removal_rate = excluded.post_removal_rate,
                 gate_penalty = excluded.gate_penalty,
                 scrutiny_score = excluded.scrutiny_score,
                 subreddit_type = excluded.subreddit_type,
                 computed_at = datetime('now')""",
            (name, subscribers, comment_removal_rate, post_removal_rate,
             gate_penalty, scrutiny_score, subreddit_type)
        )
        self.conn.commit()

    # ── Check-Live Logs ──────────────────────────────────────────────

    def log_live_check(self, comment_id, source, reddit_url, action,
                       prev_status, new_status, account_id=None,
                       subreddit=None, brand_name=None):
        self.conn.execute(
            """INSERT INTO check_live_log
               (comment_id, source, reddit_url, action, prev_status, new_status,
                account_id, subreddit, brand_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (comment_id, source, reddit_url, action, prev_status, new_status,
             account_id, subreddit, brand_name))
        self.conn.commit()

    def get_live_check_logs(self, limit=100, offset=0, action=None, source=None):
        q = "SELECT * FROM check_live_log WHERE 1=1"
        params = []
        if action:
            q += " AND action = ?"
            params.append(action)
        if source:
            q += " AND source = ?"
            params.append(source)
        q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self.conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def get_check_live_log_runs(self, hours=48, gap_minutes=5, brand_name=None):
        """Cluster recent check_live_log rows into 'runs' by time gaps so
        the operator can identify exactly which run changed statuses.

        A new run starts whenever two consecutive log rows are more than
        `gap_minutes` apart. Returns runs most-recent-first, each:
          {started_at, ended_at, count, by_action, by_new_status,
           by_source, sample}
        Timestamps are the raw check_live_log.created_at strings (UTC),
        suitable to pass straight back into revert_check_live_window().

        `brand_name` (optional, case-insensitive) narrows clustering to a
        single brand's rows — so runs[0] is that brand's most recent run.
        """
        from datetime import datetime as _dt
        where = ["created_at >= datetime('now', ?)"]
        params = [f"-{int(hours)} hours"]
        if brand_name:
            where.append("brand_name = ? COLLATE NOCASE")
            params.append(brand_name)
        rows = self.conn.execute(
            f"""SELECT comment_id, source, action, prev_status, new_status, created_at
               FROM check_live_log
               WHERE {' AND '.join(where)}
               ORDER BY created_at ASC, id ASC""",
            params
        ).fetchall()

        def _parse(ts):
            try:
                return _dt.strptime(ts, "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None

        runs = []
        cur = None
        last_t = None
        for r in rows:
            t = _parse(r["created_at"])
            new_run = (
                cur is None or
                (last_t is not None and t is not None and
                 (t - last_t).total_seconds() > gap_minutes * 60)
            )
            if new_run:
                cur = {
                    "started_at": r["created_at"], "ended_at": r["created_at"],
                    "count": 0, "by_action": {}, "by_new_status": {},
                    "by_source": {}, "sample": [],
                }
                runs.append(cur)
            cur["ended_at"] = r["created_at"]
            cur["count"] += 1
            cur["by_action"][r["action"]] = cur["by_action"].get(r["action"], 0) + 1
            cur["by_new_status"][r["new_status"]] = cur["by_new_status"].get(r["new_status"], 0) + 1
            cur["by_source"][r["source"]] = cur["by_source"].get(r["source"], 0) + 1
            if len(cur["sample"]) < 8:
                cur["sample"].append({
                    "id": r["comment_id"], "source": r["source"],
                    "action": r["action"], "from": r["prev_status"],
                    "to": r["new_status"], "at": r["created_at"],
                })
            last_t = t
        runs.reverse()
        return runs

    def revert_check_live_window(self, *, since=None, until=None, actions=None,
                                 brand_name=None, dry_run=True):
        """Revert status changes recorded in check_live_log within a time
        window back to the status each row had BEFORE the change.

        Safety rules:
          - Restores to the logged `prev_status` (the true original,
            captured for BOTH marked_dead and restored actions).
          - Only reverts a row when its CURRENT status still equals the
            logged `new_status` for that run — so a status the operator
            (or a later run) changed afterwards is left untouched.
          - Dedupes per (comment_id, source): original = the EARLIEST
            prev_status in the window; expected-current = the LATEST
            new_status in the window.
          - dry_run=True changes nothing; it returns the same report so
            the operator can preview precisely what would happen.

        `since`/`until` are UTC 'YYYY-MM-DD HH:MM:SS' strings compared
        against check_live_log.created_at. `actions` optionally limits to
        a subset of {'marked_dead','restored'}. `brand_name` (optional,
        case-insensitive) confines the revert to a single brand's rows —
        so a brand-scoped undo can never touch another brand.

        Each applied revert is itself logged (action='reverted') so the
        audit trail stays complete.
        """
        where = ["action != 'reverted'"]
        params = []
        if since:
            where.append("created_at >= ?"); params.append(since)
        if until:
            where.append("created_at <= ?"); params.append(until)
        if actions:
            qmarks = ",".join("?" for _ in actions)
            where.append(f"action IN ({qmarks})"); params.extend(actions)
        if brand_name:
            where.append("brand_name = ? COLLATE NOCASE"); params.append(brand_name)
        rows = self.conn.execute(
            f"""SELECT comment_id, source, action, prev_status, new_status, created_at
                FROM check_live_log
                WHERE {' AND '.join(where)}
                ORDER BY created_at ASC, id ASC""",
            params
        ).fetchall()

        # Group per (comment_id, source): earliest prev = true original,
        # latest new = what the row should currently be if untouched.
        groups = {}
        order = []
        for r in rows:
            k = (r["comment_id"], r["source"])
            if k not in groups:
                groups[k] = {"orig": r["prev_status"], "expected": r["new_status"]}
                order.append(k)
            else:
                groups[k]["expected"] = r["new_status"]

        report = {
            "window": {"since": since, "until": until},
            "actions": actions or ["marked_dead", "restored"],
            "dry_run": dry_run,
            "total_log_rows": len(rows),
            "affected_comments": len(groups),
            "reverted": [], "skipped": [],
            "note": ("Rows whose current status no longer matches the run's "
                     "result are skipped (changed since the run). Restoring a "
                     "row to 'paid' does not recover its original paid_at "
                     "timestamp."),
        }

        for k in order:
            cid, src = k
            g = groups[k]
            target = g["orig"]
            table = "comments" if src == "comment" else "search_comments"
            cur = self.conn.execute(
                f"SELECT status FROM {table} WHERE id = ?", (cid,)
            ).fetchone()
            if not cur:
                report["skipped"].append({"id": cid, "source": src, "reason": "row not found"})
                continue
            cur_status = cur["status"]
            if not target:
                report["skipped"].append({
                    "id": cid, "source": src, "current": cur_status,
                    "reason": "no recorded prior status to restore"})
                continue
            if cur_status != g["expected"]:
                report["skipped"].append({
                    "id": cid, "source": src, "current": cur_status,
                    "expected": g["expected"], "would_restore_to": target,
                    "reason": "current status changed since the run — left as-is"})
                continue
            if cur_status == target:
                report["skipped"].append({
                    "id": cid, "source": src, "current": cur_status,
                    "reason": "already at target"})
                continue
            report["reverted"].append({"id": cid, "source": src,
                                       "from": cur_status, "to": target})
            if not dry_run:
                self.conn.execute(
                    f"UPDATE {table} SET status = ?, prev_status = NULL WHERE id = ?",
                    (target, cid))
                try:
                    self.log_live_check(cid, src, "", "reverted", cur_status,
                                        target, brand_name=brand_name)
                except Exception:
                    pass

        if not dry_run:
            self.conn.commit()
        return report
