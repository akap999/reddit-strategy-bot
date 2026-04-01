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
                   COUNT(DISTINCT p.id) as post_count,
                   COUNT(DISTINCT c.id) as comment_count
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

    def add_brand(self, subreddit_id, name, domain_url="", context="", keywords="[]"):
        cur = self.conn.execute(
            "INSERT INTO brands (subreddit_id, name, domain_url, context, keywords) VALUES (?, ?, ?, ?, ?)",
            (subreddit_id, name, domain_url, context, keywords)
        )
        self.conn.commit()
        return cur.lastrowid

    def update_brand(self, brand_id, context=None, domain_url=None, keywords=None):
        """Update a brand's editable fields."""
        updates = []
        params = []
        if context is not None:
            updates.append("context = ?")
            params.append(context)
        if domain_url is not None:
            updates.append("domain_url = ?")
            params.append(domain_url)
        if keywords is not None:
            updates.append("keywords = ?")
            params.append(keywords)
        if not updates:
            return
        params.append(brand_id)
        self.conn.execute(f"UPDATE brands SET {', '.join(updates)} WHERE id = ?", params)
        self.conn.commit()

    def list_brands(self, subreddit_id):
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
                  suggested_post_day=0, prompt_version=None, brand_ids=None):
        cur = self.conn.execute(
            """INSERT INTO posts (subreddit_id, brand_id, title, body, storyline,
               image_prompt, image_url, ai_query_score, is_custom, is_filler,
               status, suggested_post_day, prompt_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (subreddit_id, brand_id, title, body, storyline,
             image_prompt, image_url, ai_query_score, is_custom, is_filler,
             status, suggested_post_day, prompt_version)
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

    def get_post(self, post_id):
        row = self.conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        return dict(row) if row else None

    def update_post_status(self, post_id, status):
        self.conn.execute("UPDATE posts SET status = ? WHERE id = ?", (status, post_id))
        self.conn.commit()

    def delete_subreddit(self, subreddit_id):
        """Delete a subreddit and all its underlying data (brands, posts, comments, post_urls)."""
        # Get all post IDs for this subreddit
        post_ids = [r[0] for r in self.conn.execute(
            "SELECT id FROM posts WHERE subreddit_id = ?", (subreddit_id,)
        ).fetchall()]
        if post_ids:
            placeholders = ",".join("?" * len(post_ids))
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
                SUM(CASE WHEN status = 'published' THEN 1 ELSE 0 END) as published_posts,
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
                SUM(CASE WHEN mentions_brand = 1 THEN 1 ELSE 0 END) as brand_mentions,
                AVG(validation_score) as avg_validation_score
            FROM comments c
            JOIN posts p ON c.post_id = p.id
            WHERE p.subreddit_id = ?""",
            (subreddit_id,)
        ).fetchone()
        stats["comments"] = dict(row)

        return stats

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

        # paid_at migration for search_comments
        sc_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(search_comments)").fetchall()]
        if "paid_at" not in sc_cols:
            self.conn.execute("ALTER TABLE search_comments ADD COLUMN paid_at TEXT")
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
        self.conn.execute(
            "UPDATE comments SET account_id = ?, status = 'assigned' WHERE id = ?",
            (account_id, comment_id)
        )
        self.conn.commit()

    def unassign_comment(self, comment_id):
        self.conn.execute(
            "UPDATE comments SET account_id = NULL, status = 'complete' WHERE id = ?",
            (comment_id,)
        )
        self.conn.commit()

    def deploy_comment(self, comment_id, reddit_comment_url, deployed_at=None):
        if not deployed_at:
            deployed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "UPDATE comments SET reddit_comment_url = ?, deployed_at = ?, status = 'deployed' WHERE id = ?",
            (reddit_comment_url, deployed_at, comment_id)
        )
        self.conn.commit()

    def mark_comment_deleted(self, comment_id):
        deleted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "UPDATE comments SET status = 'deleted', deleted_at = ? WHERE id = ?",
            (deleted_at, comment_id)
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

    def get_filtered_comments(self, subreddit_id, status=None, mentions_brand=None, account_id=None):
        """Get comments with post info, filtered by status/brand/account."""
        query = """SELECT c.*, p.title as post_title, p.id as p_id, pu.reddit_url as post_reddit_url
                   FROM comments c
                   JOIN posts p ON c.post_id = p.id
                   LEFT JOIN post_urls pu ON pu.post_id = p.id
                   WHERE p.subreddit_id = ?"""
        params = [subreddit_id]
        if status:
            query += " AND c.status = ?"
            params.append(status)
        if mentions_brand is not None:
            query += " AND c.mentions_brand = ?"
            params.append(1 if mentions_brand else 0)
        if account_id:
            query += " AND c.account_id = ?"
            params.append(account_id)
        query += " ORDER BY c.suggested_post_day, c.suggested_order, c.id"
        rows = self.conn.execute(query, params).fetchall()
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
        """Get all brands across all subreddits."""
        rows = self.conn.execute(
            """SELECT b.*, s.name as subreddit_name FROM brands b
               JOIN subreddits s ON b.subreddit_id = s.id
               ORDER BY b.name"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_deployed_comment_urls(self, subreddit_id):
        """Get all deployed comments with their Reddit URLs."""
        rows = self.conn.execute(
            """SELECT c.id, c.reddit_comment_url
               FROM comments c
               JOIN posts p ON c.post_id = p.id
               WHERE p.subreddit_id = ? AND c.status = 'deployed' AND c.reddit_comment_url IS NOT NULL""",
            (subreddit_id,)
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

        Optionally filtered by created_at date range.
        Returns list of dicts with comment fields + post_title, post_reddit_url, subreddit_name, subreddit_id.
        """
        query = """SELECT c.*, p.title as post_title, p.subreddit_id,
                          s.name as subreddit_name,
                          pu.reddit_url as post_reddit_url
                   FROM comments c
                   JOIN posts p ON c.post_id = p.id
                   JOIN brands b ON c.brand_id = b.id
                   JOIN subreddits s ON p.subreddit_id = s.id
                   LEFT JOIN post_urls pu ON pu.post_id = p.id
                   WHERE LOWER(b.name) = LOWER(?)"""
        params = [brand_name]
        if date_from:
            query += " AND c.created_at >= ?"
            params.append(date_from)
        if date_to:
            query += " AND c.created_at <= ?"
            params.append(date_to + " 23:59:59")
        query += " ORDER BY c.created_at DESC, c.id DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_brand_subreddit_stats(self, brand_name, date_from=None, date_to=None):
        """Get per-subreddit stats for a brand: total comments, deployed, ours, brand mentions.

        Returns list of dicts: subreddit_id, subreddit_name, total, deployed, ours, mentions_brand, deleted.
        """
        date_clause = ""
        params = [brand_name]
        if date_from:
            date_clause += " AND c.created_at >= ?"
            params.append(date_from)
        if date_to:
            date_clause += " AND c.created_at <= ?"
            params.append(date_to + " 23:59:59")

        query = f"""SELECT s.id as subreddit_id, s.name as subreddit_name,
                          COUNT(*) as total,
                          SUM(CASE WHEN c.status = 'deployed' THEN 1 ELSE 0 END) as deployed,
                          SUM(CASE WHEN c.is_ours = 1 THEN 1 ELSE 0 END) as ours,
                          SUM(CASE WHEN c.mentions_brand = 1 THEN 1 ELSE 0 END) as mentions_brand,
                          SUM(CASE WHEN c.status = 'deleted' THEN 1 ELSE 0 END) as deleted,
                          SUM(CASE WHEN c.status = 'assigned' THEN 1 ELSE 0 END) as assigned,
                          SUM(CASE WHEN c.status = 'complete' THEN 1 ELSE 0 END) as complete
                   FROM comments c
                   JOIN posts p ON c.post_id = p.id
                   JOIN brands b ON c.brand_id = b.id
                   JOIN subreddits s ON p.subreddit_id = s.id
                   WHERE LOWER(b.name) = LOWER(?) {date_clause}
                   GROUP BY s.id, s.name
                   ORDER BY total DESC"""
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_unique_brand_names(self):
        """Get distinct brand names across all subreddits with aggregated info."""
        rows = self.conn.execute(
            """SELECT b.name,
                      GROUP_CONCAT(DISTINCT s.name) as subreddit_names,
                      b.domain_url, b.context, b.keywords,
                      COUNT(DISTINCT b.subreddit_id) as num_subreddits
               FROM brands b
               JOIN subreddits s ON b.subreddit_id = s.id
               GROUP BY LOWER(b.name)
               ORDER BY b.name"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_brand_overview_stats(self, brand_name, date_from=None, date_to=None):
        """Get aggregate stats for a brand across all subreddits."""
        date_clause = ""
        params = [brand_name]
        if date_from:
            date_clause += " AND c.created_at >= ?"
            params.append(date_from)
        if date_to:
            date_clause += " AND c.created_at <= ?"
            params.append(date_to + " 23:59:59")

        row = self.conn.execute(
            f"""SELECT COUNT(*) as total_comments,
                       SUM(CASE WHEN c.status = 'deployed' THEN 1 ELSE 0 END) as deployed,
                       SUM(CASE WHEN c.is_ours = 1 THEN 1 ELSE 0 END) as ours,
                       SUM(CASE WHEN c.mentions_brand = 1 THEN 1 ELSE 0 END) as mentions_brand,
                       SUM(CASE WHEN c.status = 'deleted' THEN 1 ELSE 0 END) as deleted,
                       SUM(CASE WHEN c.status = 'assigned' THEN 1 ELSE 0 END) as assigned,
                       SUM(CASE WHEN c.status = 'complete' THEN 1 ELSE 0 END) as complete,
                       COUNT(DISTINCT p.id) as total_posts,
                       COUNT(DISTINCT p.subreddit_id) as num_subreddits
                FROM comments c
                JOIN posts p ON c.post_id = p.id
                JOIN brands b ON c.brand_id = b.id
                WHERE LOWER(b.name) = LOWER(?) {date_clause}""",
            params,
        ).fetchone()
        return dict(row) if row else {}

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
            """UPDATE accounts SET link_karma = ?, comment_karma = ?, created_utc = ?, last_refreshed = datetime('now')
               WHERE username = ?""",
            (link_karma, comment_karma, created_utc, username)
        )
        self.conn.commit()

    def update_account_reference(self, username, reference):
        self.conn.execute("UPDATE accounts SET reference = ? WHERE username = ?", (reference, username))
        self.conn.commit()

    def delete_account(self, username):
        # Unassign comments owned by this account (back to draft)
        self.conn.execute(
            "UPDATE comments SET account_id = NULL, status = 'draft' WHERE account_id = ? AND status = 'assigned'",
            (username,)
        )
        # Clear account_id on deployed/complete comments (keep their status)
        self.conn.execute(
            "UPDATE comments SET account_id = NULL WHERE account_id = ? AND status != 'assigned'",
            (username,)
        )
        # Same for search_comments
        self.conn.execute(
            "UPDATE search_comments SET account_id = NULL, status = 'draft' WHERE account_id = ? AND status = 'assigned'",
            (username,)
        )
        self.conn.execute(
            "UPDATE search_comments SET account_id = NULL WHERE account_id = ? AND status != 'assigned'",
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
                      SUM(CASE WHEN c.status = 'assigned' THEN 1 ELSE 0 END) as pending,
                      SUM(CASE WHEN c.status = 'deployed' THEN 1 ELSE 0 END) as deployed,
                      SUM(CASE WHEN c.status = 'deleted' THEN 1 ELSE 0 END) as deleted,
                      SUM(CASE WHEN c.paid_at IS NOT NULL THEN 1 ELSE 0 END) as paid,
                      SUM(CASE WHEN c.status = 'deployed' AND c.deployed_at < datetime('now', '-4 days') AND c.paid_at IS NULL AND c.deleted_at IS NULL THEN 1 ELSE 0 END) as due_payment,
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
        self.conn.execute("UPDATE posts SET owner_account = ? WHERE id = ?", (username, post_id))
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

        all_accounts = [dict(r) for r in self.conn.execute("SELECT * FROM accounts").fetchall()]

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
        account_sub_comment_counts = [dict(r) for r in self.conn.execute(
            """SELECT c.account_id, COUNT(*) as cnt
               FROM comments c JOIN posts p ON c.post_id = p.id
               WHERE p.subreddit_id = ? AND c.account_id IS NOT NULL
               GROUP BY c.account_id""",
            (subreddit_id,)
        ).fetchall()]

        return {
            "subreddit": dict(sub),
            "draft_posts": draft_posts,
            "all_accounts": all_accounts,
            "account_post_counts": account_post_counts,
            "account_sub_post_counts": account_sub_post_counts,
            "account_sub_comment_counts": account_sub_comment_counts,
        }

    def bulk_unassign_post_comments(self, post_id):
        """Unassign all assigned comments for a post, setting them back to draft."""
        self.conn.execute(
            "UPDATE comments SET account_id = NULL, status = 'draft' WHERE post_id = ? AND status = 'assigned'",
            (post_id,)
        )
        self.conn.commit()

    def unassign_post_owner(self, post_id):
        """Remove owner_account from a post."""
        self.conn.execute("UPDATE posts SET owner_account = '' WHERE id = ?", (post_id,))
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
               WHERE post_id = ? AND status = 'draft'
               ORDER BY comment_type = 'op_reply' DESC, mentions_brand DESC, suggested_post_day, suggested_order""",
            (post_id,)
        ).fetchall()]

        all_accounts = [dict(r) for r in self.conn.execute("SELECT * FROM accounts").fetchall()]

        subreddit_day_assignments = [dict(r) for r in self.conn.execute(
            """SELECT c.account_id, c.suggested_post_day, COUNT(*) as cnt
               FROM comments c JOIN posts p ON c.post_id = p.id
               WHERE p.subreddit_id = ? AND c.account_id IS NOT NULL
                 AND c.status IN ('assigned','deployed')
               GROUP BY c.account_id, c.suggested_post_day""",
            (sub_id,)
        ).fetchall()]

        account_pending_counts = [dict(r) for r in self.conn.execute(
            """SELECT account_id, COUNT(*) as cnt FROM comments
               WHERE status = 'assigned' AND account_id IS NOT NULL
               GROUP BY account_id"""
        ).fetchall()]

        account_brand_mentions = [dict(r) for r in self.conn.execute(
            """SELECT account_id, brand_id, COUNT(*) as cnt FROM comments
               WHERE mentions_brand = 1 AND account_id IS NOT NULL
               GROUP BY account_id, brand_id"""
        ).fetchall()]

        account_total_mentions = [dict(r) for r in self.conn.execute(
            """SELECT account_id, COUNT(*) as cnt FROM comments
               WHERE mentions_brand = 1 AND account_id IS NOT NULL
               GROUP BY account_id"""
        ).fetchall()]

        subreddit_veterans = [r[0] for r in self.conn.execute(
            """SELECT DISTINCT c.account_id FROM comments c JOIN posts p ON c.post_id = p.id
               WHERE p.subreddit_id = ? AND c.account_id IS NOT NULL""",
            (sub_id,)
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
                      SUM(CASE WHEN sc.status = 'assigned' THEN 1 ELSE 0 END) as pending,
                      SUM(CASE WHEN sc.status = 'deployed' THEN 1 ELSE 0 END) as deployed,
                      SUM(CASE WHEN sc.status = 'deleted' THEN 1 ELSE 0 END) as deleted,
                      SUM(CASE WHEN sc.paid_at IS NOT NULL THEN 1 ELSE 0 END) as paid,
                      SUM(CASE WHEN sc.status = 'deployed' AND sc.deployed_at < datetime('now', '-4 days') AND sc.paid_at IS NULL AND sc.deleted_at IS NULL THEN 1 ELSE 0 END) as due_payment
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
            "UPDATE comments SET paid_at = datetime('now') WHERE id = ?",
            (comment_id,)
        )
        self.conn.commit()

    def mark_search_comment_paid(self, comment_id):
        self.conn.execute(
            "UPDATE search_comments SET paid_at = datetime('now') WHERE id = ?",
            (comment_id,)
        )
        self.conn.commit()

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

    # --- Calendar Events ---

    def get_calendar_events(self, date_from=None, date_to=None, brand_id=None,
                            subreddit_id=None, account_id=None, status=None,
                            event_type=None):
        """Get unified calendar events: published posts + assigned/deployed comments."""
        queries = []
        all_params = []

        # --- Query 1: Published Posts ---
        has_posts = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='post_urls'").fetchone()
        if has_posts and (not event_type or event_type == 'post'):
            q1 = """SELECT 'post' as event_type, p.id as event_id,
                           pu.added_at as event_date,
                           p.title as title, p.body as body,
                           b.name as brand_name, b.id as brand_id,
                           s.name as subreddit_name, s.id as subreddit_id,
                           p.owner_account as account_id,
                           'published' as status,
                           pu.reddit_url as reddit_url,
                           NULL as reddit_comment_url,
                           0 as is_reply, 0 as mentions_brand,
                           NULL as reply_to_url
                    FROM posts p
                    JOIN post_urls pu ON pu.post_id = p.id
                    JOIN subreddits s ON p.subreddit_id = s.id
                    LEFT JOIN brands b ON p.brand_id = b.id
                    WHERE pu.added_at IS NOT NULL"""
            p1 = []
            if date_from:
                q1 += " AND pu.added_at >= ?"
                p1.append(date_from)
            if date_to:
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
            if status and status != 'published':
                pass  # skip posts if filtering for non-published status
            else:
                queries.append(q1)
                all_params.extend(p1)

        # --- Query 2: Comments (assigned/deployed) ---
        has_comments = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='comments'").fetchone()
        if has_comments and (not event_type or event_type in ('comment', 'search_comment')):
            if not event_type or event_type != 'search_comment':
                q2 = """SELECT 'comment' as event_type, c.id as event_id,
                               COALESCE(c.deployed_at, c.created_at) as event_date,
                               p.title as title, c.body as body,
                               b.name as brand_name, b.id as brand_id,
                               s.name as subreddit_name, s.id as subreddit_id,
                               c.account_id as account_id,
                               c.status as status,
                               pu.reddit_url as reddit_url,
                               c.reddit_comment_url as reddit_comment_url,
                               c.is_reply as is_reply, c.mentions_brand as mentions_brand,
                               NULL as reply_to_url
                        FROM comments c
                        JOIN posts p ON c.post_id = p.id
                        JOIN subreddits s ON p.subreddit_id = s.id
                        LEFT JOIN brands b ON c.brand_id = b.id
                        LEFT JOIN post_urls pu ON pu.post_id = p.id
                        WHERE c.status IN ('assigned', 'deployed')"""
                p2 = []
                if date_from:
                    q2 += " AND COALESCE(c.deployed_at, c.created_at) >= ?"
                    p2.append(date_from)
                if date_to:
                    q2 += " AND COALESCE(c.deployed_at, c.created_at) <= ?"
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
                if status:
                    q2 += " AND c.status = ?"
                    p2.append(status)
                queries.append(q2)
                all_params.extend(p2)

        # --- Query 3: Search Comments (assigned/deployed) ---
        # Check if search_comments table exists
        has_sc = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='search_comments'").fetchone()
        if has_sc and (not event_type or event_type in ('comment', 'search_comment')):
            if not event_type or event_type != 'comment':
                q3 = """SELECT 'search_comment' as event_type, sc.id as event_id,
                               COALESCE(sc.deployed_at, sc.created_at) as event_date,
                               sp.title as title, sc.body as body,
                               b.name as brand_name, b.id as brand_id,
                               sp.subreddit as subreddit_name, NULL as subreddit_id,
                               sc.account_id as account_id,
                               sc.status as status,
                               sp.reddit_url as reddit_url,
                               sc.reddit_comment_url as reddit_comment_url,
                               sc.is_reply as is_reply, sc.mentions_brand as mentions_brand,
                               sc.reply_to_url as reply_to_url
                        FROM search_comments sc
                        JOIN search_posts sp ON sc.search_post_id = sp.id
                        LEFT JOIN brands b ON sc.brand_id = b.id
                        WHERE sc.status IN ('assigned', 'deployed')"""
                p3 = []
                if date_from:
                    q3 += " AND COALESCE(sc.deployed_at, sc.created_at) >= ?"
                    p3.append(date_from)
                if date_to:
                    q3 += " AND COALESCE(sc.deployed_at, sc.created_at) <= ?"
                    p3.append(date_to + " 23:59:59")
                if brand_id:
                    q3 += " AND b.id = ?"
                    p3.append(brand_id)
                if subreddit_id:
                    # search_posts store subreddit as text name — resolve via subquery
                    q3 += " AND sp.subreddit = (SELECT name FROM subreddits WHERE id = ?)"
                    p3.append(subreddit_id)
                if account_id:
                    q3 += " AND sc.account_id = ?"
                    p3.append(account_id)
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
        q = """SELECT sp.*, b.name as brand_name
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
        self.conn.execute(
            "UPDATE search_comments SET account_id = ?, status = 'assigned' WHERE id = ?",
            (account_id, comment_id))
        self.conn.commit()

    def unassign_search_comment(self, comment_id):
        self.conn.execute(
            "UPDATE search_comments SET account_id = NULL, status = 'draft' WHERE id = ?",
            (comment_id,))
        self.conn.commit()

    def deploy_search_comment(self, comment_id, reddit_url):
        deployed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "UPDATE search_comments SET reddit_comment_url = ?, deployed_at = ?, status = 'deployed' WHERE id = ?",
            (reddit_url, deployed_at, comment_id))
        self.conn.commit()

    def update_search_comment_body(self, comment_id, body):
        self.conn.execute("UPDATE search_comments SET body = ? WHERE id = ?", (body, comment_id))
        self.conn.commit()

    def delete_search_comment(self, comment_id):
        deleted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "UPDATE search_comments SET status = 'deleted', deleted_at = ? WHERE id = ?",
            (deleted_at, comment_id))
        self.conn.commit()
