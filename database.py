import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional, Any
import crypto
import migrations

DB_PATH = Path(__file__).parent / "fb_commenter.db"

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    migrations.run_migrations(conn)
    # Perform startup recovery for dangling tasks
    recover_stale_tasks(conn)
    conn.close()

def recover_stale_tasks(conn: Optional[sqlite3.Connection] = None):
    """
    On startup or periodic check, recovers tasks that were left in 'SENDING' status
    because of unexpected crash or power-off. Moves them to 'NEED_CHECK'.
    """
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    now_iso = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute("""
            UPDATE tasks
            SET status = 'NEED_CHECK',
                error_message = 'Tiến trình bị gián đoạn khi đang gửi. Vui lòng đối chiếu trên Facebook trước khi quyết định gửi lại.',
                updated_at = ?
            WHERE status = 'SENDING'
        """, (now_iso,))
    if should_close:
        conn.close()

# --- CRUD HELPER METHODS ---

def upsert_account(account_name: str, user_id: Optional[str] = None, long_lived_user_token: Optional[str] = None) -> int:
    conn = get_connection()
    token_enc = crypto.encrypt_token(long_lived_user_token) if long_lived_user_token else None
    with conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM accounts WHERE (user_id IS NOT NULL AND user_id = ?) OR account_name = ?", (user_id, account_name))
        row = cursor.fetchone()
        if row:
            acc_id = row['id']
            cursor.execute("""
                UPDATE accounts
                SET status = 'ACTIVE',
                    user_id = COALESCE(?, user_id),
                    long_lived_user_token_enc = COALESCE(?, long_lived_user_token_enc)
                WHERE id = ?
            """, (user_id, token_enc, acc_id))
        else:
            cursor.execute("""
                INSERT INTO accounts (account_name, user_id, long_lived_user_token_enc)
                VALUES (?, ?, ?)
            """, (account_name, user_id, token_enc))
            acc_id = cursor.lastrowid
    conn.close()
    return acc_id

def upsert_page(
    page_id: str,
    page_name: str,
    page_access_token: str,
    account_id: int,
    token_status: str = "OK",
    missing_permissions: Optional[List[str]] = None,
    expires_at: Optional[str] = None
):
    conn = get_connection()
    token_enc = crypto.encrypt_token(page_access_token)
    perms_json = json.dumps(missing_permissions or [])
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute("""
            INSERT INTO pages (
                page_id, page_name, page_access_token_enc, account_id,
                is_active, token_status, missing_permissions, expires_at,
                last_synced_at, updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(page_id) DO UPDATE SET
                page_name = excluded.page_name,
                page_access_token_enc = excluded.page_access_token_enc,
                account_id = excluded.account_id,
                is_active = 1,
                token_status = excluded.token_status,
                missing_permissions = excluded.missing_permissions,
                expires_at = excluded.expires_at,
                last_synced_at = excluded.last_synced_at,
                updated_at = excluded.updated_at
        """, (page_id, page_name, token_enc, account_id, token_status, perms_json, expires_at, now, now))

        # Ensure default rule exists for this page
        conn.execute("""
            INSERT OR IGNORE INTO page_rules (page_id, delay_minutes, templates, max_comments_per_day, is_enabled)
            VALUES (?, 10, ?, 20, 1)
        """, (page_id, json.dumps(["Dạ chào bạn, inbox page để được tư vấn chi tiết nhé!"])))
    conn.close()

def get_all_pages() -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.page_id, p.page_name, p.account_id, p.is_active, p.token_status,
               p.missing_permissions, p.expires_at, p.last_synced_at, p.last_seen_post_time,
               p.last_comment_at, p.comments_today_count, p.comments_today_date,
               p.api_usage_percent, p.status_note, p.updated_at,
               a.account_name, r.delay_minutes, r.templates, r.max_comments_per_day,
               r.is_enabled as rule_enabled
        FROM pages p
        LEFT JOIN accounts a ON p.account_id = a.id
        LEFT JOIN page_rules r ON p.page_id = r.page_id
        ORDER BY p.updated_at DESC
    """)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def get_page(page_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.page_id, p.page_name, p.account_id, p.is_active, p.token_status,
               p.missing_permissions, p.expires_at, p.last_synced_at, p.last_seen_post_time,
               p.last_comment_at, p.comments_today_count, p.comments_today_date,
               p.api_usage_percent, p.status_note, p.updated_at,
               r.delay_minutes, r.templates, r.max_comments_per_day, r.is_enabled as rule_enabled
        FROM pages p
        LEFT JOIN page_rules r ON p.page_id = r.page_id
        WHERE p.page_id = ?
    """, (page_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_page_with_decrypted_token(page_id: str) -> Optional[Dict[str, Any]]:
    """Strictly for internal worker execution; decrypts token for Meta API call."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.*, r.delay_minutes, r.templates, r.max_comments_per_day, r.is_enabled as rule_enabled
        FROM pages p
        LEFT JOIN page_rules r ON p.page_id = r.page_id
        WHERE p.page_id = ?
    """, (page_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    res = dict(row)
    res["page_access_token"] = crypto.decrypt_token(res.get("page_access_token_enc", ""))
    return res

def update_page_status(page_id: str, is_active: bool, token_status: str, status_note: Optional[str] = None):
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute("""
            UPDATE pages
            SET is_active = ?, token_status = ?, status_note = ?, updated_at = ?
            WHERE page_id = ?
        """, (1 if is_active else 0, token_status, status_note, now, page_id))
    conn.close()

def update_page_rule(page_id: str, delay_minutes: int, templates: List[str], max_comments_per_day: int, is_enabled: bool):
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute("""
            INSERT INTO page_rules (page_id, delay_minutes, templates, max_comments_per_day, is_enabled, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(page_id) DO UPDATE SET
                delay_minutes = excluded.delay_minutes,
                templates = excluded.templates,
                max_comments_per_day = excluded.max_comments_per_day,
                is_enabled = excluded.is_enabled,
                updated_at = excluded.updated_at
        """, (page_id, delay_minutes, json.dumps(templates, ensure_ascii=False), max_comments_per_day, 1 if is_enabled else 0, now))
    conn.close()

def create_task(
    page_id: str,
    post_id: str,
    comment_content: str,
    scheduled_at: str,
    source: str = "AUTO",
    batch_id: Optional[str] = None,
    post_created_time: Optional[str] = None,
    dedupe_key: Optional[str] = None
) -> bool:
    """
    Creates a task using dedupe_key.
    For AUTO tasks: 'auto:{page_id}:{post_id}'
    For MANUAL tasks: 'manual:{batch_id}:{post_id}'
    """
    if not dedupe_key:
        if source == "AUTO":
            dedupe_key = f"auto:{page_id}:{post_id}"
        else:
            bid = batch_id or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            dedupe_key = f"manual:{bid}:{post_id}"

    conn = get_connection()
    try:
        with conn:
            conn.execute("""
                INSERT INTO tasks (page_id, post_id, dedupe_key, source, batch_id, comment_content, scheduled_at, post_created_time, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """, (page_id, post_id, dedupe_key, source, batch_id, comment_content, scheduled_at, post_created_time))
        return True
    except sqlite3.IntegrityError:
        # Task already exists with this dedupe_key
        return False
    finally:
        conn.close()

def get_due_tasks(limit: int = 10) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    now_utc = datetime.now(timezone.utc).isoformat()
    cursor.execute("""
        SELECT t.*, p.page_access_token_enc, p.page_name, p.last_comment_at,
               p.comments_today_count, p.comments_today_date, r.max_comments_per_day
        FROM tasks t
        JOIN pages p ON t.page_id = p.page_id
        LEFT JOIN page_rules r ON p.page_id = r.page_id
        WHERE t.status = 'PENDING' AND t.scheduled_at <= ? AND p.is_active = 1
        ORDER BY t.scheduled_at ASC
        LIMIT ?
    """, (now_utc, limit))
    rows = []
    for r in cursor.fetchall():
        d = dict(r)
        d["page_access_token"] = crypto.decrypt_token(d.pop("page_access_token_enc", ""))
        rows.append(d)
    conn.close()
    return rows

def lock_task_for_sending(task_id: int) -> bool:
    conn = get_connection()
    with conn:
        cursor = conn.cursor()
        now_iso = datetime.now(timezone.utc).isoformat()
        cursor.execute("""
            UPDATE tasks
            SET status = 'SENDING', locked_at = ?, updated_at = ?
            WHERE id = ? AND status = 'PENDING'
        """, (now_iso, now_iso, task_id))
        affected = cursor.rowcount > 0
    conn.close()
    return affected

def update_task_result(task_id: int, status: str, comment_id: Optional[str] = None, error_message: Optional[str] = None):
    conn = get_connection()
    now_utc = datetime.now(timezone.utc).isoformat()
    with conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tasks
            SET status = ?, comment_id = ?, error_message = ?, updated_at = ?,
                retry_count = retry_count + (CASE WHEN ? = 'FAILED' THEN 1 ELSE 0 END)
            WHERE id = ?
        """, (status, comment_id, error_message, now_utc, status, task_id))

        if status == "SUCCESS":
            # Update Page stats: last_comment_at, comments_today_count
            cursor.execute("SELECT page_id FROM tasks WHERE id = ?", (task_id,))
            t_row = cursor.fetchone()
            if t_row:
                pid = t_row["page_id"]
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                cursor.execute("""
                    UPDATE pages
                    SET last_comment_at = ?,
                        comments_today_count = (CASE WHEN comments_today_date = ? THEN comments_today_count + 1 ELSE 1 END),
                        comments_today_date = ?
                    WHERE page_id = ?
                """, (now_utc, today_str, today_str, pid))
    conn.close()

def cancel_task(task_id: int) -> bool:
    conn = get_connection()
    with conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tasks SET status = 'CANCELLED', updated_at = ?
            WHERE id = ? AND status IN ('PENDING', 'PAUSED')
        """, (datetime.now(timezone.utc).isoformat(), task_id))
        success = cursor.rowcount > 0
    conn.close()
    return success

def get_all_tasks(page_id: Optional[str] = None, status: Optional[str] = None, source: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    query = """
        SELECT t.id, t.page_id, t.post_id, t.dedupe_key, t.source, t.batch_id,
               t.post_created_time, t.comment_content, t.scheduled_at, t.status,
               t.locked_at, t.comment_id, t.error_message, t.retry_count, t.updated_at,
               p.page_name
        FROM tasks t
        JOIN pages p ON t.page_id = p.page_id
        WHERE 1=1
    """
    params = []
    if page_id:
        query += " AND t.page_id = ?"
        params.append(page_id)
    if status:
        query += " AND t.status = ?"
        params.append(status)
    if source:
        query += " AND t.source = ?"
        params.append(source)
    query += " ORDER BY t.id DESC LIMIT ?"
    params.append(limit)

    cursor.execute(query, params)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows
