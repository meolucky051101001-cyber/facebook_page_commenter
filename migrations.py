import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
import crypto

def run_migrations(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur = conn.cursor()
    cur.execute("SELECT MAX(version) FROM schema_version")
    row = cur.fetchone()
    current_version = row[0] if row and row[0] is not None else 0

    if current_version < 1:
        _apply_migration_v1(conn)
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.commit()

    if current_version < 2:
        _apply_migration_v2(conn)
        conn.execute("INSERT INTO schema_version (version) VALUES (2)")
        conn.commit()


def _apply_migration_v2(conn: sqlite3.Connection):
    """Adds Antidetect Python integration columns to pages and tasks."""
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(pages)")
    p_cols = [col[1] for col in cur.fetchall()]
    page_new_cols = {
        "connection_mode": "TEXT DEFAULT 'GRAPH_API'",
        "antidetect_profile_id": "TEXT",
        "antidetect_profile_name": "TEXT",
        "page_url": "TEXT",
        "fb_profile_id": "TEXT",
    }
    for col_name, col_def in page_new_cols.items():
        if col_name not in p_cols:
            conn.execute(f"ALTER TABLE pages ADD COLUMN {col_name} {col_def}")

    cur.execute("PRAGMA table_info(tasks)")
    t_cols = [col[1] for col in cur.fetchall()]
    if "post_url" not in t_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN post_url TEXT")


def _apply_migration_v1(conn: sqlite3.Connection):
    # Create accounts table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL,
            user_id TEXT UNIQUE,
            long_lived_user_token_enc TEXT,
            status TEXT DEFAULT 'ACTIVE',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Check if old pages table exists
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pages'")
    old_pages_exists = cur.fetchone() is not None

    if not old_pages_exists:
        conn.execute("""
            CREATE TABLE pages (
                page_id TEXT PRIMARY KEY,
                page_name TEXT NOT NULL,
                page_access_token_enc TEXT NOT NULL,
                account_id INTEGER REFERENCES accounts(id),
                is_active INTEGER DEFAULT 1,
                token_status TEXT DEFAULT 'OK',
                missing_permissions TEXT,
                expires_at TIMESTAMP,
                last_synced_at TIMESTAMP,
                last_seen_post_time TIMESTAMP,
                last_comment_at TIMESTAMP,
                comments_today_count INTEGER DEFAULT 0,
                comments_today_date TEXT,
                api_usage_percent REAL DEFAULT 0.0,
                status_note TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        # Check columns and migrate
        cur.execute("PRAGMA table_info(pages)")
        cols = [col[1] for col in cur.fetchall()]
        if "page_access_token" in cols and "page_access_token_enc" not in cols:
            conn.execute("ALTER TABLE pages RENAME COLUMN page_access_token TO page_access_token_enc")
        # Add new columns if missing
        new_cols = {
            "token_status": "TEXT DEFAULT 'OK'",
            "missing_permissions": "TEXT",
            "expires_at": "TIMESTAMP",
            "last_seen_post_time": "TIMESTAMP",
            "last_comment_at": "TIMESTAMP",
            "comments_today_count": "INTEGER DEFAULT 0",
            "comments_today_date": "TEXT",
            "api_usage_percent": "REAL DEFAULT 0.0"
        }
        for col_name, col_def in new_cols.items():
            if col_name not in cols:
                conn.execute(f"ALTER TABLE pages ADD COLUMN {col_name} {col_def}")

    # Check page_rules table
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='page_rules'")
    if not cur.fetchone():
        conn.execute("""
            CREATE TABLE page_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id TEXT UNIQUE REFERENCES pages(page_id),
                delay_minutes INTEGER DEFAULT 10,
                templates TEXT NOT NULL DEFAULT '["Cảm ơn bạn đã quan tâm! Vui lòng inbox để được hỗ trợ nhanh nhất."]',
                max_comments_per_day INTEGER DEFAULT 20,
                is_enabled INTEGER DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        cur.execute("PRAGMA table_info(page_rules)")
        cols = [col[1] for col in cur.fetchall()]
        if "max_comments_per_day" not in cols:
            conn.execute("ALTER TABLE page_rules ADD COLUMN max_comments_per_day INTEGER DEFAULT 20")

    # Tasks table with dedupe_key
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
    tasks_exists = cur.fetchone() is not None

    if not tasks_exists:
        conn.execute("""
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id TEXT NOT NULL REFERENCES pages(page_id),
                post_id TEXT NOT NULL,
                dedupe_key TEXT UNIQUE NOT NULL,
                source TEXT DEFAULT 'AUTO',
                batch_id TEXT,
                post_created_time TIMESTAMP,
                comment_content TEXT NOT NULL,
                scheduled_at TIMESTAMP NOT NULL,
                status TEXT CHECK(status IN ('PENDING', 'SENDING', 'SUCCESS', 'FAILED', 'NEED_CHECK', 'CANCELLED', 'PAUSED')) DEFAULT 'PENDING',
                locked_at TIMESTAMP,
                comment_id TEXT,
                error_message TEXT,
                retry_count INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_time ON tasks(status, scheduled_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_page ON tasks(page_id)")
    else:
        cur.execute("PRAGMA table_info(tasks)")
        t_cols = [col[1] for col in cur.fetchall()]
        if "dedupe_key" not in t_cols:
            # Recreate tasks table with dedupe_key safely
            conn.execute("""
                CREATE TABLE tasks_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    page_id TEXT NOT NULL REFERENCES pages(page_id),
                    post_id TEXT NOT NULL,
                    dedupe_key TEXT UNIQUE NOT NULL,
                    source TEXT DEFAULT 'AUTO',
                    batch_id TEXT,
                    post_created_time TIMESTAMP,
                    comment_content TEXT NOT NULL,
                    scheduled_at TIMESTAMP NOT NULL,
                    status TEXT CHECK(status IN ('PENDING', 'SENDING', 'SUCCESS', 'FAILED', 'NEED_CHECK', 'CANCELLED', 'PAUSED')) DEFAULT 'PENDING',
                    locked_at TIMESTAMP,
                    comment_id TEXT,
                    error_message TEXT,
                    retry_count INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Copy old data generating dedupe_key as 'auto:{page_id}:{post_id}'
            conn.execute("""
                INSERT OR IGNORE INTO tasks_new (id, page_id, post_id, dedupe_key, source, post_created_time, comment_content, scheduled_at, status, comment_id, error_message, retry_count, updated_at)
                SELECT id, page_id, post_id, ('auto:' || page_id || ':' || post_id), 'AUTO', post_created_time, comment_content, scheduled_at, status, comment_id, error_message, retry_count, updated_at
                FROM tasks
            """)
            conn.execute("DROP TABLE tasks")
            conn.execute("ALTER TABLE tasks_new RENAME TO tasks")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_time ON tasks(status, scheduled_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_page ON tasks(page_id)")
        elif "locked_at" not in t_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN locked_at TIMESTAMP")
