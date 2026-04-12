"""SQLite persistence layer for Reddit Strategy Bot."""

import sqlite3
import json
import os
import re
from datetime import datetime


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

    def list_subreddits(self):
        rows = self.conn.execute("""
            SELECT s.*,
                   COUNT(DISTINCT b.id) as brand_count,
                   COUNT(DISTINCT CASE WHEN p.status IN ('published', 'paid') THEN p.id END) as post_count,
                   COUNT(DISTINCT CASE WHEN c.status IN ('deployed', 'paid') THEN c.id END) as comment_count
            FROM subreddits s
            LEFT JOIN brands b ON b.subreddit_id = s.id
            LEFT JOIN posts p ON p.subreddit_id = s.id
            LEFT JOIN comments c ON c.post_id = p.id
            GROUP BY s.id
            ORDER BY s.created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]

    def get_subreddit(self, subreddit_id):
        row = self.conn.execute("SELECT * FROM subreddits WHERE id = ?", (subreddit_id,)).fetchone()
        return dict(row) if row else None

    def get_subreddit_by_name(self, name):
        row = self.conn.execute("SELECT * FROM subreddits WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    # --- Brands ---

    def add_brand(self, subreddit_id, name, domain_url="", context="", keywords="[]",
                  category=None, audience=None, use_cases=None, pain_points=None,
                  features=None, competitors=None, enriched_at=None):
        cur = self.conn.execute(
            """INSERT INTO brands (subreddit_id, name, domain_url, context, keywords,
                                   category, audience, use_cases, pain_points,
                                   features, competitors, enriched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (subreddit_id, name, domain_url, context, keywords,
             category, audience, use_cases, pain_points,
             features, competitors, enriched_at)
        )
        self.conn.commit()
        return cur.lastrowid

    def update_brand(self, brand_id, context=None, domain_url=None, keywords=None,
                     category=None, audience=None, use_cases=None, pain_points=None,
                     features=None, competitors=None, enriched_at=None):
        """Update a brand's editable fields. Pass only the fields you want to change."""
        updates = []
        params = []
        field_map = {
            "context": context, "domain_url": domain_url, "keywords": keywords,
            "category": category, "audience": audience, "use_cases": use_cases,
            "pain_points": pain_points, "features": features,
            "competitors": competitors, "enriched_at": enriched_at,
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
        """Link a Reddit URL to a generated post (after manual publishing)."""
        existing = self.conn.execute(
            "SELECT id FROM post_urls WHERE reddit_url = ?", (reddit_url,)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE post_urls SET post_id = ? WHERE reddit_url = ?",
                (post_id, reddit_url)
            )
        else:
            self.conn.execute(
                "INSERT INTO post_urls (subreddit_id, reddit_url, post_id) VALUES (?, ?, ?)",
                (subreddit_id, reddit_url, post_id)
            )
        self.conn.commit()

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
                  intent=None):
        cur = self.conn.execute(
            """INSERT INTO posts (subreddit_id, brand_id, title, body, storyline,
               image_prompt, image_url, ai_query_score, is_custom, is_filler,
               status, suggested_post_day, prompt_version, intent)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (subreddit_id, brand_id, title, body, storyline,
             image_prompt, image_url, ai_query_score, is_custom, is_filler,
             status, suggested_post_day, prompt_version, intent)
        )
        post_id = cur.lastrowid
        # Populate junction table
        ids = brand_ids or ([brand_id] if brand_id else [])
        if ids:
            self.add_post_brands(post_id, ids)
        self.conn.commit()
        return post_id

    def get_posts(self, subreddit_id, brand_id=None, limit=50, include_filler=True):
        if brand_id is not None:
            query = """SELECT DISTINCT p.* FROM posts p
                       JOIN post_brands pb ON pb.post_id = p.id
                       WHERE p.subreddit_id = ? AND pb.brand_id = ?"""
            params = [subreddit_id, brand_id]
        else:
            query = "SELECT * FROM posts WHERE subreddit_id = ?"
            params = [subreddit_id]
        if not include_filler:
            query += " AND is_filler = 0"
        query += " ORDER BY suggested_post_day ASC, created_at DESC LIMIT ?"
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
            if not p.get("brand_names"):
                p["brand_names"] = ""
            if not p.get("reddit_url"):
                p["reddit_url"] = ""
            results.append(p)
        return results

    def get_all_posts(self, brand_id=None, subreddit_id=None, status=None, date=None, limit=200):
        """Get all posts across all subreddits with comment counts."""
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
            if not p.get("brand_names"):
                p["brand_names"] = ""
            if not p.get("reddit_url"):
                p["reddit_url"] = ""
            results.append(p)
        return results

    def get_post(self, post_id):
        row = self.conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        return dict(row) if row else None

    def update_post_status(self, post_id, status):
        self.conn.execute("UPDATE posts SET status = ? WHERE id = ?", (status, post_id))
        self.conn.commit()

    def undeploy_post(self, post_id):
        """Revert a deployed (published) post back to complete status."""
        self.conn.execute(
            """UPDATE posts SET status = 'complete', deployed_at = NULL, paid_at = NULL
               WHERE id = ? AND status IN ('published', 'paid')""",
            (post_id,)
        )
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
        """Returns all post titles across ALL subreddits for any brand with this name."""
        rows = self.conn.execute(
            """SELECT DISTINCT p.title FROM posts p
               JOIN post_brands pb ON pb.post_id = p.id
               JOIN brands b ON pb.brand_id = b.id
               WHERE LOWER(b.name) = LOWER(?)""",
            (brand_name,)
        ).fetchall()
        return [r["title"] for r in rows]

    # --- Comments ---

    def save_comment(self, post_id, brand_id, body, persona_id=None,
                     structure_id=None, is_reply=0, parent_comment_id=None,
                     mentions_brand=0, validation_score=None, account_id=None,
                     status="draft", suggested_post_day=0, suggested_order=0,
                     prompt_version=None, comment_type=""):
        cur = self.conn.execute(
            """INSERT INTO comments (post_id, brand_id, body, persona_id, structure_id,
               is_reply, parent_comment_id, mentions_brand, validation_score, account_id,
               status, suggested_post_day, suggested_order, prompt_version, comment_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (post_id, brand_id, body, persona_id, structure_id,
             is_reply, parent_comment_id, mentions_brand, validation_score, account_id,
             status, suggested_post_day, suggested_order, prompt_version, comment_type)
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
        # Free the rotation slot for any child replies + this comment itself,
        # for every row that had a non-null account_id and wasn't already a draft.
        rows = self.conn.execute(
            """SELECT account_id, COUNT(*) AS cnt FROM comments
               WHERE (id = ? OR parent_comment_id = ?)
                 AND account_id IS NOT NULL
                 AND status != 'draft'
               GROUP BY account_id""",
            (comment_id, comment_id)
        ).fetchall()
        for r in rows:
            self._decrement_lifetime(r["account_id"], r["cnt"])
        # Delete child replies first
        self.conn.execute("DELETE FROM comments WHERE parent_comment_id = ?", (comment_id,))
        self.conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
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
                SUM(CASE WHEN c.paid_at IS NOT NULL THEN 1 ELSE 0 END) as paid,
                SUM(CASE WHEN c.paid_at IS NULL THEN 1 ELSE 0 END) as unpaid
            FROM comments c
            JOIN posts p ON c.post_id = p.id
            {where_sql} {"AND" if where_sql else "WHERE"} c.status = 'deployed' AND c.account_id IS NOT NULL
            GROUP BY c.account_id
            ORDER BY total DESC
        """, params).fetchall()]

        # Per-brand breakdown: subreddits, posts, comments (branded/general) deployed
        bq_parts = ["c.status = 'deployed'"]
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
                SUM(CASE WHEN c.paid_at IS NOT NULL THEN 1 ELSE 0 END) as paid_comments,
                SUM(CASE WHEN c.paid_at IS NULL THEN 1 ELSE 0 END) as unpaid_comments
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
                SUM(CASE WHEN c.paid_at IS NOT NULL THEN 1 ELSE 0 END) as paid_comments,
                SUM(CASE WHEN c.paid_at IS NULL THEN 1 ELSE 0 END) as unpaid_comments
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
        }.items():
            if col not in sc_cols:
                self.conn.execute(sql)
                self.conn.commit()

        # paid_at migration for posts
        post_cols2 = [r[1] for r in self.conn.execute("PRAGMA table_info(posts)").fetchall()]
        if "paid_at" not in post_cols2:
            self.conn.execute("ALTER TABLE posts ADD COLUMN paid_at TEXT")
            self.conn.commit()
        if "deployed_at" not in post_cols2:
            self.conn.execute("ALTER TABLE posts ADD COLUMN deployed_at TEXT")
            self.conn.commit()

        # Migrate existing paid items: set status='paid' where paid_at is set
        self.conn.execute("UPDATE comments SET status = 'paid' WHERE paid_at IS NOT NULL AND status != 'paid'")
        self.conn.execute("UPDATE search_comments SET status = 'paid' WHERE paid_at IS NOT NULL AND status != 'paid'")
        self.conn.execute("UPDATE posts SET status = 'paid' WHERE paid_at IS NOT NULL AND status != 'paid'")
        self.conn.commit()

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
            "category":    "ALTER TABLE brands ADD COLUMN category TEXT",
            "audience":    "ALTER TABLE brands ADD COLUMN audience TEXT",
            "use_cases":   "ALTER TABLE brands ADD COLUMN use_cases TEXT",
            "pain_points": "ALTER TABLE brands ADD COLUMN pain_points TEXT",
            "features":    "ALTER TABLE brands ADD COLUMN features TEXT",
            "competitors": "ALTER TABLE brands ADD COLUMN competitors TEXT",
            "enriched_at": "ALTER TABLE brands ADD COLUMN enriched_at TEXT",
        }
        for col, sql in brand_enrichment_cols.items():
            if col not in brand_cols:
                self.conn.execute(sql)
                self.conn.commit()

        # ----- posts: intent column for GEO-style 1:1:1 batches -----
        post_cols3 = [r[1] for r in self.conn.execute("PRAGMA table_info(posts)").fetchall()]
        if "intent" not in post_cols3:
            self.conn.execute("ALTER TABLE posts ADD COLUMN intent TEXT")
            self.conn.commit()

        # ----- app_meta: small key/value store for one-time startup flags -----
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS app_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
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

    def get_task(self, task_id):
        row = self.conn.execute(
            "SELECT id, type, status, result, error FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "status": row["status"],
            "type": row["type"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": row["error"]
        }

    def cleanup_old_tasks(self, hours=24):
        self.conn.execute(
            "DELETE FROM tasks WHERE created_at < datetime('now', ?)",
            (f'-{hours} hours',)
        )
        self.conn.commit()

    def mark_comment_ours(self, comment_id, is_ours):
        self.conn.execute("UPDATE comments SET is_ours = ? WHERE id = ?", (1 if is_ours else 0, comment_id))
        self.conn.commit()

    # --- Comment Lifecycle ---

    def assign_comment(self, comment_id, account_id):
        # Read prior account so we can correctly adjust the lifetime counter
        # if the comment was already assigned to someone else (reassignment).
        row = self.conn.execute(
            "SELECT account_id FROM comments WHERE id = ?", (comment_id,)
        ).fetchone()
        prior = row["account_id"] if row else None
        self.conn.execute(
            "UPDATE comments SET account_id = ?, status = 'assigned', assigned_at = datetime('now') WHERE id = ?",
            (account_id, comment_id)
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

    def deploy_comment(self, comment_id, reddit_comment_url, deployed_at=None):
        if not deployed_at:
            deployed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "UPDATE comments SET reddit_comment_url = ?, deployed_at = ?, status = 'deployed' WHERE id = ?",
            (reddit_comment_url, deployed_at, comment_id)
        )
        self.conn.commit()

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
            "UPDATE comments SET status = 'informed', informed_at = datetime('now') WHERE id = ? AND status = 'assigned'",
            (comment_id,)
        )
        self.conn.commit()

    def mark_comment_deleted(self, comment_id):
        deleted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "UPDATE comments SET status = 'deleted', deleted_at = ? WHERE id = ?",
            (deleted_at, comment_id)
        )
        self.conn.commit()

    def mark_comment_removed(self, comment_id):
        """Mark a deployed comment as removed/deleted on Reddit."""
        self.conn.execute(
            "UPDATE comments SET status = 'removed' WHERE id = ? AND status IN ('deployed', 'paid')",
            (comment_id,)
        )
        self.conn.commit()

    def unremove_comment(self, comment_id):
        """Revert a removed comment back to deployed status."""
        self.conn.execute(
            "UPDATE comments SET status = 'deployed' WHERE id = ? AND status = 'removed'",
            (comment_id,)
        )
        self.conn.commit()

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
                       c.is_ours, c.matched_keywords,
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
                       1 as is_ours, NULL as matched_keywords,
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

    def get_all_comments_by_brand(self, brand_id, status=None, sort_by=None):
        """Get all comments (regular + search) for a brand across all subreddits."""
        status_filter_reg = "AND c.status = ?" if status else "AND c.status != 'deleted'"
        status_filter_sc = "AND sc.status = ?" if status else "AND sc.status != 'deleted'"

        q1 = f"""SELECT c.id, c.body, c.status, c.account_id, c.brand_id,
                        c.is_reply, c.mentions_brand, c.created_at, c.deployed_at,
                        c.paid_at, c.reddit_comment_url, c.comment_type,
                        c.suggested_post_day, c.suggested_order,
                        c.is_ours, c.matched_keywords,
                        'comment' as source,
                        p.title as post_title, p.id as p_id,
                        s.name as subreddit_name,
                        (SELECT pu.reddit_url FROM post_urls pu WHERE pu.post_id = p.id LIMIT 1) as post_reddit_url
                 FROM comments c
                 JOIN posts p ON c.post_id = p.id
                 LEFT JOIN subreddits s ON p.subreddit_id = s.id
                 WHERE c.brand_id = ? {status_filter_reg}"""
        p1 = [brand_id]
        if status:
            p1.append(status)

        q2 = f"""SELECT sc.id, sc.body, sc.status, sc.account_id, sc.brand_id,
                        sc.is_reply, sc.mentions_brand, sc.created_at, sc.deployed_at,
                        sc.paid_at, sc.reddit_comment_url, NULL as comment_type,
                        0 as suggested_post_day, 0 as suggested_order,
                        1 as is_ours, NULL as matched_keywords,
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

        query = f"SELECT * FROM ({q1} UNION ALL {q2}) combined {order}"
        rows = self.conn.execute(query, p1 + p2).fetchall()
        return [dict(r) for r in rows]

    def get_all_comments_global(self, status=None, brand_id=None, subreddit_id=None,
                                account_id=None, sort_by=None, source=None,
                                date=None, limit=200, offset=0):
        """Get all comments (regular + search) globally with optional filters and pagination."""
        # Build WHERE clauses dynamically
        w1, w2 = ["c.status NOT IN ('deleted','archived')"], ["sc.status NOT IN ('deleted','archived')"]
        p1, p2 = [], []

        if status:
            w1 = ["c.status = ?"]; p1.append(status)
            w2 = ["sc.status = ?"]; p2.append(status)
        if brand_id:
            w1.append("c.brand_id = ?"); p1.append(brand_id)
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
                        c.paid_at, c.reddit_comment_url, c.comment_type,
                        c.suggested_post_day, c.suggested_order,
                        c.is_ours, c.matched_keywords, c.assigned_at, c.informed_at,
                        c.last_live_check,
                        'comment' as source,
                        p.title as post_title, p.id as p_id,
                        s.name as subreddit_name, b.name as brand_name,
                        (SELECT pu.reddit_url FROM post_urls pu WHERE pu.post_id = p.id LIMIT 1) as post_reddit_url
                 FROM comments c
                 JOIN posts p ON c.post_id = p.id
                 LEFT JOIN subreddits s ON p.subreddit_id = s.id
                 LEFT JOIN brands b ON c.brand_id = b.id
                 WHERE {where1}"""

        q2 = f"""SELECT sc.id, sc.body, sc.status, sc.account_id, sc.brand_id,
                        sc.is_reply, sc.mentions_brand, sc.created_at, sc.deployed_at,
                        sc.paid_at, sc.reddit_comment_url, NULL as comment_type,
                        0 as suggested_post_day, 0 as suggested_order,
                        1 as is_ours, NULL as matched_keywords, sc.assigned_at, sc.informed_at,
                        sc.last_live_check,
                        'search_comment' as source,
                        sp.title as post_title, sp.id as p_id,
                        sp.subreddit as subreddit_name, b.name as brand_name,
                        sp.reddit_url as post_reddit_url
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

        # Source filter: only one table if specified
        if source == 'comment':
            inner = q1
            all_params = p1
        elif source == 'search_comment':
            inner = q2
            all_params = p2
        else:
            inner = f"{q1} UNION ALL {q2}"
            all_params = p1 + p2

        union = f"SELECT * FROM ({inner}) combined {order}"

        # Count
        count_query = f"SELECT COUNT(*) as cnt FROM ({inner}) combined"
        total = self.conn.execute(count_query, all_params).fetchone()["cnt"]

        # Paginated results
        paginated = f"{union} LIMIT ? OFFSET ?"
        rows = self.conn.execute(paginated, all_params + [limit, offset]).fetchall()
        return {"items": [dict(r) for r in rows], "total": total}

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
        """Get deployed search comments with their Reddit URLs for live checking."""
        rows = self.conn.execute(
            """SELECT sc.id, sc.reddit_comment_url
               FROM search_comments sc
               WHERE sc.status = 'deployed' AND sc.reddit_comment_url IS NOT NULL"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_deployed_comment_urls(self):
        """Get all deployed comment URLs across both tables for live checking."""
        rows = self.conn.execute(
            """SELECT c.id, c.reddit_comment_url, 'comment' as source
               FROM comments c
               WHERE c.status = 'deployed' AND c.reddit_comment_url IS NOT NULL
               UNION ALL
               SELECT sc.id, sc.reddit_comment_url, 'search_comment' as source
               FROM search_comments sc
               WHERE sc.status = 'deployed' AND sc.reddit_comment_url IS NOT NULL"""
        ).fetchall()
        return [dict(r) for r in rows]

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
                      SUM(CASE WHEN c.paid_at IS NOT NULL THEN 1 ELSE 0 END) as paid,
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
            "SELECT owner_account FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        prior = row["owner_account"] if row else None
        self.conn.execute("UPDATE posts SET owner_account = ? WHERE id = ?", (username, post_id))
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
            "UPDATE posts SET owner_account = '' WHERE subreddit_id = ? AND status NOT IN ('published', 'informed') AND owner_account IS NOT NULL AND owner_account != ''",
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
            "SELECT owner_account FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        prior = row["owner_account"] if row else None
        self.conn.execute("UPDATE posts SET owner_account = '' WHERE id = ?", (post_id,))
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
                      SUM(CASE WHEN sc.paid_at IS NOT NULL THEN 1 ELSE 0 END) as paid,
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
        self.conn.execute(
            "UPDATE comments SET status = 'paid', paid_at = datetime('now') WHERE id = ?",
            (comment_id,)
        )
        self.conn.commit()

    def mark_post_paid(self, post_id):
        self.conn.execute(
            "UPDATE posts SET status = 'paid', paid_at = datetime('now') WHERE id = ?",
            (post_id,)
        )
        self.conn.commit()

    def mark_search_comment_paid(self, comment_id):
        self.conn.execute(
            "UPDATE search_comments SET status = 'paid', paid_at = datetime('now') WHERE id = ?",
            (comment_id,)
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
        paid_clause = ""
        if paid_filter == 'paid':
            paid_clause = "AND paid_at IS NOT NULL"
        elif paid_filter == 'unpaid':
            paid_clause = "AND paid_at IS NULL"

        # Build WHERE for comments
        c_where = ["c.status = 'deployed'"]
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
        sc_where2 = ["sc.status = 'deployed'"]
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
                WHERE {c_where_sql} {paid_clause.replace('paid_at', 'c.paid_at')}

                UNION ALL

                SELECT 'post' as type, p.id, p.title as body, p.owner_account as account_id,
                       p.deployed_at, p.paid_at, 0 as mentions_brand, p.title as post_title,
                       GROUP_CONCAT(DISTINCT b2.name) as brand_name, s.name as subreddit_name
                FROM posts p
                LEFT JOIN post_brands pb ON pb.post_id = p.id
                LEFT JOIN brands b2 ON b2.id = pb.brand_id
                LEFT JOIN subreddits s ON p.subreddit_id = s.id
                WHERE {p_where_sql} {paid_clause.replace('paid_at', 'p.paid_at')}
                GROUP BY p.id

                UNION ALL

                SELECT 'search_comment' as type, sc.id, sc.body, sc.account_id, sc.deployed_at,
                       sc.paid_at, sc.mentions_brand, sp.title as post_title,
                       b.name as brand_name, sp.subreddit as subreddit_name
                FROM search_comments sc
                LEFT JOIN search_posts sp ON sc.search_post_id = sp.id
                LEFT JOIN brands b ON sc.brand_id = b.id
                WHERE {sc_where_sql} {paid_clause.replace('paid_at', 'sc.paid_at')}
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
                WHERE {c_where_sql} {paid_clause.replace('paid_at', 'c.paid_at')}
            ) + (
                SELECT COUNT(*) FROM posts p
                WHERE {p_where_sql} {paid_clause.replace('paid_at', 'p.paid_at')}
            ) + (
                SELECT COUNT(*) FROM search_comments sc
                WHERE {sc_where_sql} {paid_clause.replace('paid_at', 'sc.paid_at')}
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
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # duplicate URL

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

    # --- Search Comments (Live Search) ---

    def add_search_comment(self, search_post_id, body, brand_id=None, persona_id=None,
                           is_reply=0, reply_to_url=None, mentions_brand=0, relevance_score=None):
        cur = self.conn.execute(
            """INSERT INTO search_comments
               (search_post_id, body, brand_id, persona_id, is_reply, reply_to_url, mentions_brand, relevance_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (search_post_id, body, brand_id, persona_id, is_reply, reply_to_url, mentions_brand, relevance_score)
        )
        self.conn.commit()
        return cur.lastrowid

    def list_search_comments(self, search_post_id=None, status=None):
        q = """SELECT sc.*, sp.title as post_title, sp.subreddit as post_subreddit,
                      sp.reddit_url as post_url, b.name as brand_name
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
            "SELECT account_id FROM search_comments WHERE id = ?", (comment_id,)
        ).fetchone()
        prior = row["account_id"] if row else None
        self.conn.execute(
            "UPDATE search_comments SET account_id = ?, status = 'assigned', assigned_at = datetime('now') WHERE id = ?",
            (account_id, comment_id))
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

    def deploy_search_comment(self, comment_id, reddit_url):
        deployed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "UPDATE search_comments SET reddit_comment_url = ?, deployed_at = ?, status = 'deployed' WHERE id = ?",
            (reddit_url, deployed_at, comment_id))
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
            "UPDATE search_comments SET status = 'informed', informed_at = datetime('now') WHERE id = ? AND status = 'assigned'",
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
            "UPDATE search_comments SET status = 'deleted', deleted_at = ? WHERE id = ?",
            (deleted_at, comment_id))
        # Only decrement if this row was counted toward lifetime (i.e. had an
        # account attached and wasn't already deleted).
        if prior and prior_status != 'deleted':
            self._decrement_lifetime(prior)
        self.conn.commit()

    def mark_search_comment_removed(self, comment_id):
        """Mark a deployed search comment as removed/deleted on Reddit."""
        self.conn.execute(
            "UPDATE search_comments SET status = 'removed' WHERE id = ? AND status IN ('deployed', 'paid')",
            (comment_id,))
        self.conn.commit()

    def unremove_search_comment(self, comment_id):
        """Revert a removed search comment back to deployed status."""
        self.conn.execute(
            "UPDATE search_comments SET status = 'deployed' WHERE id = ? AND status = 'removed'",
            (comment_id,))
        self.conn.commit()

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
