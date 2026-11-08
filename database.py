import shutil
import sqlite3
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Any
import config
import crypto
import migrations

DB_PATH = Path(__file__).parent / "fb_commenter.db"


def local_day_key(dt_utc: datetime) -> str:
    """Returns the counter date for the configured local day (default UTC+7)."""
    return (dt_utc + timedelta(hours=config.DAILY_COUNTER_UTC_OFFSET_HOURS)).strftime("%Y-%m-%d")


def next_daily_reset_utc(dt_utc: datetime) -> str:
    """Returns the next local midnight expressed in UTC ISO format."""
    offset = timedelta(hours=config.DAILY_COUNTER_UTC_OFFSET_HOURS)
    local_next = (dt_utc + offset + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (local_next - offset).isoformat()


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    # Backup existing DB file if present before running migrations
    if DB_PATH.exists() and DB_PATH.stat().st_size > 0:
        backup_path = DB_PATH.with_suffix(".db.bak")
        try:
            shutil.copy2(DB_PATH, backup_path)
        except Exception:
            pass

    conn = get_connection()
    migrations.run_migrations(conn)
    # Perform startup recovery for dangling tasks from previous process crash
    recover_stale_tasks(conn, startup=True)
    conn.close()


def recover_stale_tasks(
    conn: Optional[sqlite3.Connection] = None,
    startup: bool = True,
    stale_after_seconds: int = 300
) -> int:
    """
    Recovers tasks left in 'SENDING' status:
    - If startup=True: recovers ALL tasks in 'SENDING' (process just restarted).
    - If startup=False: only recovers tasks in 'SENDING' whose locked_at is older
      than `stale_after_seconds` (default 5 minutes), avoiding active in-flight tasks!
    """
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    affected = 0
    with conn:
        cursor = conn.cursor()
        if startup:
            cursor.execute("""
                UPDATE tasks
                SET status = 'NEED_CHECK',
                    error_message = 'Tiến trình bị gián đoạn khi đang gửi. Vui lòng đối chiếu trên Facebook trước khi quyết định gửi lại.',
                    updated_at = ?
                WHERE status = 'SENDING'
            """, (now_iso,))
            affected = cursor.rowcount
        else:
            cutoff_iso = (now_utc - timedelta(seconds=stale_after_seconds)).isoformat()
            cursor.execute("""
                UPDATE tasks
                SET status = 'NEED_CHECK',
                    error_message = 'Tác vụ bị treo ở trạng thái Đang gửi quá 5 phút. Vui lòng đối chiếu trên Facebook.',
                    updated_at = ?
                WHERE status = 'SENDING' AND (locked_at IS NULL OR locked_at <= ?)
            """, (now_iso, cutoff_iso))
            affected = cursor.rowcount

    if should_close:
        conn.close()
    return affected


# --- CRUD HELPER METHODS ---

def upsert_account(account_name: str, user_id: Optional[str] = None, long_lived_user_token: Optional[str] = None) -> int:
    conn = get_connection()
    token_enc = crypto.encrypt_token(long_lived_user_token) if long_lived_user_token else None
    with conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM accounts WHERE (user_id IS NOT NULL AND user_id = ?) OR account_name = ?",
            (user_id, account_name)
        )
        row = cursor.fetchone()
        if row:
            acc_id = row['id']
            cursor.execute("""
                UPDATE accounts
                SET status = 'ACTIVE',
                    account_name = ?,
                    user_id = COALESCE(?, user_id),
                    long_lived_user_token_enc = COALESCE(?, long_lived_user_token_enc)
                WHERE id = ?
            """, (account_name, user_id, token_enc, acc_id))
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
    expires_at: Optional[str] = None,
    status_note: Optional[str] = None
):
    conn = get_connection()
    token_enc = crypto.encrypt_token(page_access_token)
    perms_json = json.dumps(missing_permissions or [])
    is_active = 0 if token_status in ("EXPIRED", "MISSING_PERMISSIONS") else 1
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute("""
            INSERT INTO pages (
                page_id, page_name, page_access_token_enc, account_id,
                is_active, token_status, missing_permissions, expires_at,
                last_synced_at, status_note, connection_mode, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'GRAPH_API', ?)
            ON CONFLICT(page_id) DO UPDATE SET
                page_name = excluded.page_name,
                page_access_token_enc = excluded.page_access_token_enc,
                account_id = excluded.account_id,
                is_active = excluded.is_active,
                token_status = excluded.token_status,
                missing_permissions = excluded.missing_permissions,
                expires_at = excluded.expires_at,
                last_synced_at = excluded.last_synced_at,
                status_note = excluded.status_note,
                connection_mode = (CASE WHEN pages.antidetect_profile_id IS NOT NULL AND pages.antidetect_profile_id != '' THEN 'HYBRID' ELSE 'GRAPH_API' END),
                updated_at = excluded.updated_at
        """, (page_id, page_name, token_enc, account_id, is_active, token_status, perms_json, expires_at, now, status_note, now))

        # Ensure default rule exists for this page
        conn.execute("""
            INSERT OR IGNORE INTO page_rules (page_id, delay_minutes, templates, max_comments_per_day, is_enabled)
            VALUES (?, 10, ?, 20, 1)
        """, (page_id, json.dumps(["Dạ chào bạn, inbox page để được tư vấn chi tiết nhé!"], ensure_ascii=False)))
    conn.close()


def upsert_antidetect_page(
    fb_page_id: str,
    page_name: str,
    page_url: str,
    antidetect_profile_id: str,
    antidetect_profile_name: str,
    account_id: int
) -> Dict[str, Any]:
    """
    Links or inserts a Fanpage discovered from Antidetect Python.
    - If a Page already exists in `pages` by page_id, fb_profile_id, or matching page_name
      (with an existing Graph API token), links the Antidetect profile and sets connection_mode='HYBRID'.
    - Otherwise inserts/updates as connection_mode='ANTIDETECT', token_status='ANTIDETECT_READY'.
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    clean_name = (page_name or "").strip()
    result_action = "inserted"
    target_page_id = fb_page_id

    with conn:
        cur = conn.cursor()
        # 1. Check exact match on page_id or fb_profile_id
        cur.execute(
            "SELECT page_id, page_access_token_enc, token_status FROM pages WHERE page_id = ? OR fb_profile_id = ?",
            (fb_page_id, fb_page_id)
        )
        existing = cur.fetchone()

        # 2. If not found by ID, check if a Graph API page with the exact same name exists
        if not existing and clean_name:
            cur.execute("""
                SELECT page_id, page_access_token_enc, token_status
                FROM pages
                WHERE LOWER(TRIM(page_name)) = LOWER(?)
                  AND page_access_token_enc IS NOT NULL
                  AND page_access_token_enc != ''
                LIMIT 1
            """, (clean_name,))
            existing = cur.fetchone()

        if existing:
            target_page_id = existing["page_id"]
            has_token = bool(existing["page_access_token_enc"])
            new_mode = "HYBRID" if has_token else "ANTIDETECT"
            new_token_status = existing["token_status"] if has_token else "ANTIDETECT_READY"
            conn.execute("""
                UPDATE pages
                SET antidetect_profile_id = ?,
                    antidetect_profile_name = ?,
                    page_url = COALESCE(?, page_url),
                    fb_profile_id = COALESCE(?, fb_profile_id),
                    connection_mode = ?,
                    token_status = ?,
                    last_synced_at = ?,
                    updated_at = ?
                WHERE page_id = ?
            """, (
                antidetect_profile_id,
                antidetect_profile_name,
                page_url,
                fb_page_id,
                new_mode,
                new_token_status,
                now,
                now,
                target_page_id
            ))
            result_action = "linked_hybrid" if has_token else "updated"
        else:
            conn.execute("""
                INSERT INTO pages (
                    page_id, page_name, page_access_token_enc, account_id,
                    is_active, token_status, missing_permissions,
                    last_synced_at, status_note, connection_mode,
                    antidetect_profile_id, antidetect_profile_name,
                    page_url, fb_profile_id, updated_at
                )
                VALUES (?, ?, '', ?, 1, 'ANTIDETECT_READY', '[]', ?, 'Kết nối qua Antidetect CDP', 'ANTIDETECT', ?, ?, ?, ?, ?)
            """, (
                fb_page_id,
                clean_name or f"Page {fb_page_id}",
                account_id,
                now,
                antidetect_profile_id,
                antidetect_profile_name,
                page_url,
                fb_page_id,
                now
            ))
            result_action = "inserted"

        # Ensure default rule exists for this page
        conn.execute("""
            INSERT OR IGNORE INTO page_rules (page_id, delay_minutes, templates, max_comments_per_day, is_enabled)
            VALUES (?, 10, ?, 20, 1)
        """, (
            target_page_id,
            json.dumps(
                ["{Dạ chào bạn|Chào bạn nhé|Xin chào bạn}, {inbox page để được tư vấn chi tiết nhé|nhắn tin cho shop để được hỗ trợ ngay nha}!"],
                ensure_ascii=False
            )
        ))
    conn.close()
    return {"page_id": target_page_id, "action": result_action}


def get_all_pages() -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.page_id, p.page_name, p.account_id, p.is_active, p.token_status,
               p.missing_permissions, p.expires_at, p.last_synced_at, p.last_seen_post_time,
               p.last_comment_at, p.comments_today_count, p.comments_today_date,
               p.api_usage_percent, p.status_note, p.updated_at,
               p.connection_mode, p.antidetect_profile_id, p.antidetect_profile_name,
               p.page_url, p.fb_profile_id,
               a.account_name, r.delay_minutes, r.templates, r.max_comments_per_day,
               r.is_enabled as rule_enabled
        FROM pages p
        LEFT JOIN accounts a ON p.account_id = a.id
        LEFT JOIN page_rules r ON p.page_id = r.page_id
        ORDER BY p.updated_at DESC, p.page_name ASC
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
               p.connection_mode, p.antidetect_profile_id, p.antidetect_profile_name,
               p.page_url, p.fb_profile_id,
               r.delay_minutes, r.templates, r.max_comments_per_day, r.is_enabled as rule_enabled
        FROM pages p
        LEFT JOIN page_rules r ON p.page_id = r.page_id
        WHERE p.page_id = ? OR p.fb_profile_id = ?
    """, (page_id, page_id))
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
        if not is_active and token_status in ("EXPIRED", "MISSING_PERMISSIONS"):
            # Pause all pending tasks of this page so they don't keep failing
            conn.execute("""
                UPDATE tasks
                SET status = 'PAUSED',
                    error_message = COALESCE(?, 'Tạm dừng do Token của Page hết hạn hoặc thiếu quyền'),
                    updated_at = ?
                WHERE page_id = ? AND status = 'PENDING'
            """, (status_note, now, page_id))
    conn.close()


def update_page_api_usage(page_id: str, usage_pct: float):
    conn = get_connection()
    with conn:
        conn.execute("""
            UPDATE pages SET api_usage_percent = ? WHERE page_id = ?
        """, (float(usage_pct), page_id))
    conn.close()


def resume_paused_tasks_for_page(page_id: str) -> int:
    """Resumes PAUSED tasks for a page back to PENDING, shifting overdue times to now."""
    conn = get_connection()
    now_iso = datetime.now(timezone.utc).isoformat()
    with conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tasks
            SET status = 'PENDING',
                scheduled_at = (CASE WHEN scheduled_at < ? THEN ? ELSE scheduled_at END),
                error_message = NULL,
                updated_at = ?
            WHERE page_id = ? AND status = 'PAUSED'
        """, (now_iso, now_iso, now_iso, page_id))
        count = cursor.rowcount
    conn.close()
    return count


def update_page_rule(
    page_id: str,
    delay_minutes: int,
    templates: List[str],
    max_comments_per_day: int = 20,
    is_enabled: bool = True,
    conn: Optional[sqlite3.Connection] = None
) -> int:
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    now = datetime.now(timezone.utc).isoformat()
    cancelled_auto = 0
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

        if not is_enabled:
            cursor = conn.execute("""
                UPDATE tasks
                SET status = 'CANCELLED', updated_at = ?
                WHERE page_id = ? AND status = 'PENDING' AND source = 'AUTO'
            """, (now, page_id))
            cancelled_auto = cursor.rowcount

    if should_close:
        conn.close()
    return cancelled_auto


def create_task(
    page_id: str,
    post_id: str,
    comment_content: str,
    scheduled_at: str,
    source: str = "AUTO",
    batch_id: Optional[str] = None,
    post_created_time: Optional[str] = None,
    dedupe_key: Optional[str] = None,
    post_url: Optional[str] = None
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
            bid = batch_id or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
            dedupe_key = f"manual:{bid}:{post_id}"

    conn = get_connection()
    try:
        with conn:
            conn.execute("""
                INSERT INTO tasks (page_id, post_id, post_url, dedupe_key, source, batch_id, comment_content, scheduled_at, post_created_time, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """, (page_id, post_id, post_url, dedupe_key, source, batch_id, comment_content, scheduled_at, post_created_time))
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def get_due_tasks(limit: int = 10) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    now_utc = datetime.now(timezone.utc).isoformat()
    cursor.execute("""
        SELECT t.*, p.page_access_token_enc, p.page_name, p.last_comment_at,
               p.comments_today_count, p.comments_today_date,
               p.connection_mode, p.antidetect_profile_id, p.antidetect_profile_name,
               p.page_url, p.fb_profile_id,
               r.max_comments_per_day
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


def release_task_lock(task_id: int) -> bool:
    """Returns a locked-but-not-sent task to PENDING (used when an emergency pause lands)."""
    conn = get_connection()
    with conn:
        cursor = conn.cursor()
        now_iso = datetime.now(timezone.utc).isoformat()
        cursor.execute("""
            UPDATE tasks
            SET status = 'PENDING', locked_at = NULL, updated_at = ?
            WHERE id = ? AND status = 'SENDING'
        """, (now_iso, task_id))
        released = cursor.rowcount > 0
    conn.close()
    return released


def update_task_result(task_id: int, status: str, comment_id: Optional[str] = None, error_message: Optional[str] = None):
    conn = get_connection()
    now_dt = datetime.now(timezone.utc)
    now_utc = now_dt.isoformat()
    with conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tasks
            SET status = ?, comment_id = ?, error_message = ?, updated_at = ?,
                retry_count = retry_count + (CASE WHEN ? = 'FAILED' THEN 1 ELSE 0 END)
            WHERE id = ?
        """, (status, comment_id, error_message, now_utc, status, task_id))

        if status == "SUCCESS":
            cursor.execute("SELECT page_id FROM tasks WHERE id = ?", (task_id,))
            t_row = cursor.fetchone()
            if t_row:
                pid = t_row["page_id"]
                today_str = local_day_key(now_dt)
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


def retry_task(task_id: int) -> bool:
    """
    Safely resets a task back to PENDING only if it is currently in
    FAILED, NEED_CHECK, PAUSED, or CANCELLED status.
    Never retries a task that is already SUCCESS or SENDING!
    """
    conn = get_connection()
    now_iso = datetime.now(timezone.utc).isoformat()
    with conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tasks
            SET status = 'PENDING',
                scheduled_at = ?,
                retry_count = 0,
                locked_at = NULL,
                error_message = NULL,
                updated_at = ?
            WHERE id = ? AND status IN ('FAILED', 'NEED_CHECK', 'PAUSED', 'CANCELLED')
        """, (now_iso, now_iso, task_id))
        success = cursor.rowcount > 0
    conn.close()
    return success


def resolve_task(task_id: int, comment_id: Optional[str] = None) -> bool:
    """
    Manually marks a NEED_CHECK / FAILED / PAUSED task as SUCCESS after operator verification.
    """
    conn = get_connection()
    now_iso = datetime.now(timezone.utc).isoformat()
    with conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tasks
            SET status = 'SUCCESS',
                comment_id = ?,
                updated_at = ?
            WHERE id = ? AND status IN ('NEED_CHECK', 'FAILED', 'PAUSED')
        """, (comment_id or "MANUAL_VERIFIED", now_iso, task_id))
        success = cursor.rowcount > 0
        if success:
            cursor.execute("SELECT page_id FROM tasks WHERE id = ?", (task_id,))
            t_row = cursor.fetchone()
            if t_row:
                today_str = local_day_key(datetime.now(timezone.utc))
                cursor.execute("""
                    UPDATE pages
                    SET last_comment_at = ?,
                        comments_today_count = (CASE WHEN comments_today_date = ? THEN comments_today_count + 1 ELSE 1 END),
                        comments_today_date = ?
                    WHERE page_id = ?
                """, (now_iso, today_str, today_str, t_row["page_id"]))
    conn.close()
    return success


def get_all_tasks(page_id: Optional[str] = None, status: Optional[str] = None, source: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    query = """
        SELECT t.id, t.page_id, t.post_id, t.post_url, t.dedupe_key, t.source, t.batch_id,
               t.post_created_time, t.comment_content, t.scheduled_at, t.status,
               t.locked_at, t.comment_id, t.error_message, t.retry_count, t.updated_at,
               p.page_name, p.connection_mode, p.antidetect_profile_name
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
