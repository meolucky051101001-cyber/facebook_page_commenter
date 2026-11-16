from contextlib import closing, nullcontext
import sqlite3
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Any
import config
import crypto
import migrations
import comment_content
import post_identity

DB_PATH = Path(__file__).parent / "fb_commenter.db"


def browser_identity_id(page: Dict[str, Any]) -> str:
    """Numeric Page identity selected for browser work; names never establish identity."""
    for value in (page.get("fb_profile_id"), page.get("page_id")):
        value = str(value or "").strip()
        if value and value.isascii() and value.isdigit():
            return value
    return ""


def _selected_transport_ready_sql(alias: str = "p") -> str:
    """SQL companion of comment_dispatch.send_ready; caller binds pilot Page ID."""
    identity = f"""CASE
        WHEN COALESCE({alias}.fb_profile_id, '') <> '' AND {alias}.fb_profile_id NOT GLOB '*[^0-9]*'
            THEN {alias}.fb_profile_id
        WHEN {alias}.page_id <> '' AND {alias}.page_id NOT GLOB '*[^0-9]*' THEN {alias}.page_id
        ELSE '' END"""
    return f"""(
        ({alias}.send_mode = 'GRAPH_API'
         AND {alias}.token_status IN ('OK', 'EXPIRING_SOON') AND {alias}.page_access_token_enc <> ''
         AND ({alias}.page_id = ? OR ({alias}.connection_checked_at IS NOT NULL AND {alias}.connection_error IS NULL)))
        OR ({alias}.send_mode = 'BROWSER' AND {alias}.browser_status = 'READY'
            AND {alias}.browser_checked_at IS NOT NULL AND {alias}.browser_error IS NULL
            AND COALESCE({alias}.antidetect_profile_id, '') <> ''
            AND {alias}.browser_profile_id = {alias}.antidetect_profile_id
            AND COALESCE({alias}.browser_identity_id, '') <> ''
            AND {alias}.browser_identity_id = ({identity}))
    )"""


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
    # SQLite produces a consistent snapshot, including committed WAL data.
    if DB_PATH.exists() and DB_PATH.stat().st_size > 0:
        backup_dir = DB_PATH.parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = backup_dir / f"{DB_PATH.stem}-{stamp}.sqlite3"
        with closing(sqlite3.connect(DB_PATH)) as source, closing(sqlite3.connect(backup_path)) as backup:
            source.backup(backup)
            if backup.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RuntimeError("Kiểm tra bản sao SQLite thất bại.")

    conn = get_connection()
    migrations.run_migrations(conn, preserve_auto_page_id=config.PILOT_PAGE_ID)
    # Perform startup recovery for dangling tasks from previous process crash
    recover_stale_tasks(conn, startup=True)
    conn.close()


def get_setting(setting_key: str, default: str = "") -> str:
    conn = get_connection()
    row = conn.execute(
        "SELECT setting_value FROM app_settings WHERE setting_key = ?",
        (setting_key,),
    ).fetchone()
    conn.close()
    return str(row[0]) if row else default


def set_setting(setting_key: str, setting_value: str) -> None:
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute("""
            INSERT INTO app_settings (setting_key, setting_value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value = excluded.setting_value,
                updated_at = excluded.updated_at
        """, (setting_key, setting_value, now))
    conn.close()


def update_page_connection_check(
    page_id: str,
    checked_at: str,
    error: Optional[str] = None,
    token_status: Optional[str] = None,
    missing_permissions: Optional[List[str]] = None,
    expires_at: Optional[str] = None,
) -> None:
    """Records a read-only connection check without reactivating a paused Page."""
    conn = get_connection()
    with conn:
        if token_status is None:
            conn.execute("""
                UPDATE pages
                SET connection_checked_at = ?, connection_error = ?, updated_at = ?
                WHERE page_id = ?
            """, (checked_at, error, checked_at, page_id))
        else:
            conn.execute("""
                UPDATE pages
                SET connection_checked_at = ?, connection_error = ?, token_status = ?,
                    missing_permissions = ?, expires_at = ?, status_note = ?, updated_at = ?
                WHERE page_id = ?
            """, (
                checked_at,
                error,
                token_status,
                json.dumps(missing_permissions or [], ensure_ascii=False),
                expires_at,
                error,
                checked_at,
                page_id,
            ))
            if token_status in ("EXPIRED", "MISSING_PERMISSIONS", "IDENTITY_MISMATCH", "INVALID_SECRET"):
                conn.execute("""
                    UPDATE pages SET is_active = 0 WHERE page_id = ? AND send_mode = 'GRAPH_API'
                """, (page_id,))
                conn.execute("""
                    UPDATE tasks SET status = 'PAUSED', error_message = ?, updated_at = ?
                    WHERE page_id = ? AND status = 'PENDING'
                      AND EXISTS (SELECT 1 FROM pages p WHERE p.page_id = tasks.page_id AND p.send_mode = 'GRAPH_API')
                """, (error or "Tạm dừng do kết nối Page không hợp lệ.", checked_at, page_id))
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
    is_active = 0 if token_status in ("EXPIRED", "MISSING_PERMISSIONS", "UNVERIFIED", "IDENTITY_MISMATCH") else 1
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
                connection_checked_at = NULL,
                connection_error = NULL,
                account_id = excluded.account_id,
                is_active = pages.is_active,
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
            VALUES (?, 10, ?, 20, 0)
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
    - If a Page already exists in `pages` by page_id
      (with an existing Graph API token), links the Antidetect profile and sets connection_mode='HYBRID'.
    - Otherwise inserts/updates as connection_mode='ANTIDETECT', token_status='ANTIDETECT_READY'.
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    clean_name = (page_name or "").strip()
    result_action = "inserted"
    target_page_id = fb_page_id

    with closing(conn), conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        cur.execute(
            """SELECT page_id, page_access_token_enc, token_status, fb_profile_id, antidetect_profile_id FROM pages
               WHERE page_id = ? OR fb_profile_id = ?""",
            (fb_page_id, fb_page_id)
        )
        matches = cur.fetchall()
        if len(matches) > 1:
            return {"action": "conflict", "page_id": fb_page_id}
        existing = matches[0] if matches else None

        if existing:
            target_page_id = existing["page_id"]
            mapping_changed = existing["antidetect_profile_id"] != antidetect_profile_id
            if mapping_changed and conn.execute(
                "SELECT 1 FROM tasks WHERE page_id=? AND status='SENDING' LIMIT 1", (target_page_id,)
            ).fetchone():
                return {"action": "conflict", "page_id": target_page_id}
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
                existing["fb_profile_id"] or fb_page_id,
                new_mode,
                new_token_status,
                now,
                now,
                target_page_id
            ))
            result_action = "linked_hybrid" if has_token else "updated"
            if mapping_changed:
                conn.execute("""
                    UPDATE pages SET browser_status='UNCHECKED', browser_checked_at=NULL,
                        browser_error=NULL, browser_identity_id=NULL, browser_profile_id=NULL
                    WHERE page_id=?
                """, (target_page_id,))
                conn.execute("""
                    UPDATE tasks SET status='PAUSED', error_message=?, updated_at=?
                    WHERE page_id=? AND status='PENDING'
                      AND EXISTS (SELECT 1 FROM pages p WHERE p.page_id=tasks.page_id AND p.send_mode='BROWSER')
                """, ("Profile trình duyệt đã đổi. Kiểm tra kết nối rồi tiếp tục tác vụ.", now, target_page_id))
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
            VALUES (?, 10, ?, 20, 0)
        """, (
            target_page_id,
            json.dumps(
                ["{Dạ chào bạn|Chào bạn nhé|Xin chào bạn}, {inbox page để được tư vấn chi tiết nhé|nhắn tin cho shop để được hỗ trợ ngay nha}!"],
                ensure_ascii=False
            )
        ))
    return {"page_id": target_page_id, "action": result_action}


def get_all_pages() -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.page_id, p.page_name, p.account_id, p.is_active, p.token_status,
               (p.page_access_token_enc <> '') AS has_page_token,
               p.missing_permissions, p.expires_at, p.last_synced_at, p.last_seen_post_time,
               p.last_comment_at, p.comments_today_count, p.comments_today_date,
               p.api_usage_percent, p.status_note, p.updated_at, p.send_cooldown_until, p.send_backoff_attempts,
               p.connection_mode, p.antidetect_profile_id, p.antidetect_profile_name,
               p.page_url, p.fb_profile_id,
               a.account_name, r.delay_minutes, r.templates, r.comment_sequence, r.content_revision, r.max_comments_per_day,
               r.is_enabled as rule_enabled, r.source_error, r.source_synced_at,
               p.connection_checked_at, p.connection_error,
               p.send_mode, p.browser_status, p.browser_checked_at, p.browser_error,
               p.browser_identity_id, p.browser_profile_id
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
               p.api_usage_percent, p.status_note, p.updated_at, p.send_cooldown_until, p.send_backoff_attempts,
               p.connection_mode, p.antidetect_profile_id, p.antidetect_profile_name,
               p.page_url, p.fb_profile_id,
               r.delay_minutes, r.templates, r.comment_sequence, r.content_revision, r.max_comments_per_day, r.is_enabled as rule_enabled,
               r.source_error, r.source_synced_at, p.connection_checked_at, p.connection_error,
               p.send_mode, p.browser_status, p.browser_checked_at, p.browser_error,
               p.browser_identity_id, p.browser_profile_id
        FROM pages p
        LEFT JOIN page_rules r ON p.page_id = r.page_id
        WHERE p.page_id = ? OR p.fb_profile_id = ?
    """, (page_id, page_id))
    rows = cursor.fetchall()
    conn.close()
    return dict(rows[0]) if len(rows) == 1 else None



def get_page_with_decrypted_token(page_id: str) -> Optional[Dict[str, Any]]:
    """Strictly for internal worker execution; decrypts token for Meta API call."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.*, r.delay_minutes, r.templates, r.comment_sequence, r.content_revision, r.max_comments_per_day, r.is_enabled as rule_enabled,
               r.source_error, r.source_synced_at
        FROM pages p
        LEFT JOIN page_rules r ON p.page_id = r.page_id
        WHERE p.page_id = ?
    """, (page_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    res = dict(row)
    try:
        res["page_access_token"] = crypto.decrypt_token(res.get("page_access_token_enc", ""))
    except (crypto.TokenDecryptionError, ValueError):
        res["page_access_token"] = ""
        res["token_status"] = "INVALID_SECRET"
        res["status_note"] = "Không giải mã được token Page. Kiểm tra khóa hoặc kết nối lại."
    return res


def invalidate_page_token(page_id: str, encrypted_token: str) -> bool:
    """Quarantine only the token that failed, without overwriting a reconnect."""
    now = datetime.now(timezone.utc).isoformat()
    message = "Không giải mã được token Page. Kiểm tra khóa mã hóa hoặc kết nối lại."
    with closing(get_connection()) as conn, conn:
        changed = conn.execute("""
            UPDATE pages SET token_status = 'INVALID_SECRET', status_note = ?, updated_at = ?
            WHERE page_id = ? AND page_access_token_enc = ?
        """, (message, now, page_id, encrypted_token)).rowcount
        if changed:
            conn.execute("""
                UPDATE tasks SET status = 'PAUSED', error_message = ?, updated_at = ?
                WHERE page_id = ? AND status = 'PENDING'
                  AND EXISTS (SELECT 1 FROM pages p WHERE p.page_id=tasks.page_id AND p.send_mode='GRAPH_API')
            """, (message, now, page_id))
        return bool(changed)


def get_page_for_dispatch(page_id: str, *, transport: str = "selected") -> Optional[Dict[str, Any]]:
    if transport not in ("selected", "GRAPH_API"):
        raise ValueError("Cách gửi bình luận không hợp lệ.")
    page = get_page(page_id)
    if not page:
        return None
    if transport == "selected" and page.get("send_mode") == "BROWSER":
        # Browser operation never depends on the encryption key or a Graph token.
        page["page_access_token"] = ""
        return page
    page_id = page["page_id"]
    page = get_page_with_decrypted_token(page_id)
    if page and page["token_status"] == "INVALID_SECRET":
        if not invalidate_page_token(page_id, page.get("page_access_token_enc", "")):
            # A reconnect won the race. Re-read it on the next dispatch pass.
            return None
    return page


def set_page_send_mode(page_id: str, send_mode: str) -> Dict[str, Any]:
    """Select a transport explicitly, preserving consent, history, counters and cursors."""
    if send_mode not in ("GRAPH_API", "BROWSER"):
        raise ValueError("Chọn GRAPH_API hoặc BROWSER.")
    now = datetime.now(timezone.utc).isoformat()
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT * FROM pages WHERE page_id=? OR fb_profile_id=?", (page_id, page_id)).fetchall()
        if len(rows) != 1:
            raise ValueError("Không tìm thấy duy nhất một Page.")
        page = dict(rows[0])
        page_id = page["page_id"]
        if page["send_mode"] != send_mode:
            if conn.execute("SELECT 1 FROM tasks WHERE page_id=? AND status='SENDING' LIMIT 1", (page_id,)).fetchone():
                raise ValueError("Page đang gửi bình luận. Chờ hoàn tất rồi đổi cách gửi.")
            if send_mode == "BROWSER" and (not page.get("antidetect_profile_id") or not browser_identity_id(page)):
                raise ValueError("Page cần profile Antidetect và ID Page dạng số đã xác định.")
            conn.execute("""
                UPDATE pages SET send_mode=?, browser_status='UNCHECKED', browser_checked_at=NULL,
                    browser_error=NULL, browser_identity_id=NULL, browser_profile_id=NULL, updated_at=?
                WHERE page_id=?
            """, (send_mode, now, page_id))
            conn.execute("""
                UPDATE tasks SET status='PAUSED', error_message=?, updated_at=?
                WHERE page_id=? AND status='PENDING'
            """, ("Cách gửi đã đổi. Kiểm tra kết nối rồi tiếp tục tác vụ.", now, page_id))
    return get_page(page_id)


def update_page_browser_check(page_id: str, checked_at: Optional[str] = None,
                              browser_status: str = "UNCHECKED", error: Optional[str] = None,
                              identity_id: Optional[str] = None, profile_id: Optional[str] = None,
                              expected_generation: Optional[int] = None) -> bool:
    """Persist proof only for the currently selected profile and numeric Page identity."""
    if browser_status not in ("UNCHECKED", "READY", "LOGIN_REQUIRED", "CHECKPOINT", "RESTRICTED", "ERROR"):
        raise ValueError("Trạng thái kết nối trình duyệt không hợp lệ.")
    checked_at = checked_at or datetime.now(timezone.utc).isoformat()
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM pages WHERE page_id=?", (page_id,)).fetchone()
        if not row or row["send_mode"] != "BROWSER":
            return False
        page = dict(row)
        expected_identity = browser_identity_id(page)
        if identity_id is not None and identity_id != expected_identity:
            return False
        if profile_id is not None and profile_id != page.get("antidetect_profile_id"):
            return False
        if browser_status == "READY" and (error or not expected_identity or not profile_id or not identity_id):
            return False
        if browser_status == "READY":
            import browser_safety
            safety = browser_safety.status(profile_id)
            if safety["browser_status"] != "READY" or (expected_generation is not None and safety["generation"] != expected_generation):
                return False
        conn.execute("""
            UPDATE pages SET browser_status=?, browser_checked_at=?, browser_error=?,
                browser_identity_id=?, browser_profile_id=?, updated_at=? WHERE page_id=?
        """, (browser_status, checked_at, error or None,
               identity_id if browser_status == "READY" else None,
               profile_id if browser_status == "READY" else None, checked_at, page_id))
        if browser_status != "READY":
            conn.execute("""
                UPDATE tasks SET status='PAUSED', error_message=?, updated_at=?
                WHERE page_id=? AND status='PENDING'
            """, (error or "Cần kiểm tra kết nối trình duyệt của Page.", checked_at, page_id))
    return True


def invalidate_page_browser(page_id: str, browser_status: str = "ERROR", error: Optional[str] = None,
                            *, profile_id: Optional[str] = None, identity_id: Optional[str] = None) -> bool:
    if browser_status == "READY":
        raise ValueError("Dùng kiểm tra kết nối để xác nhận trình duyệt sẵn sàng.")
    return update_page_browser_check(page_id, browser_status=browser_status, error=error,
                                     profile_id=profile_id, identity_id=identity_id)


def update_page_status(page_id: str, is_active: bool, token_status: str, status_note: Optional[str] = None):
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute("""
            UPDATE pages
            SET is_active = CASE WHEN send_mode='BROWSER' AND ?=0
                    AND ? IN ('EXPIRED', 'MISSING_PERMISSIONS', 'INVALID_SECRET', 'IDENTITY_MISMATCH')
                    THEN is_active ELSE ? END,
                token_status = ?, status_note = ?, updated_at = ?
            WHERE page_id = ?
        """, (1 if is_active else 0, token_status, 1 if is_active else 0, token_status, status_note, now, page_id))
        if token_status in ("EXPIRED", "MISSING_PERMISSIONS", "INVALID_SECRET"):
            # Pause all pending tasks of this page so they don't keep failing
            conn.execute("""
                UPDATE tasks
                SET status = 'PAUSED',
                    error_message = COALESCE(?, 'Tạm dừng do Token của Page hết hạn hoặc thiếu quyền'),
                    updated_at = ?
                WHERE page_id = ? AND status = 'PENDING'
                  AND EXISTS (SELECT 1 FROM pages p WHERE p.page_id=tasks.page_id AND p.send_mode='GRAPH_API')
            """, (status_note, now, page_id))
    conn.close()


def update_page_api_usage(page_id: str, usage_pct: float):
    conn = get_connection()
    with conn:
        conn.execute("""
            UPDATE pages SET api_usage_percent = ? WHERE page_id = ?
        """, (float(usage_pct), page_id))
    conn.close()


def get_send_cooldown(page: Dict[str, Any], now: datetime) -> Optional[str]:
    deadlines = [page.get("send_cooldown_until")]
    for key in ("send_cooldown:APP", f"send_cooldown:ACCOUNT:{page.get('account_id')}"):
        deadlines.append(json.loads(get_setting(key, "{}")).get("until"))
    future = [value for value in deadlines if value and datetime.fromisoformat(value) > now]
    return max(future, key=datetime.fromisoformat) if future else None


def record_rate_limit(page_id: str, result: Dict[str, Any]) -> str:
    now = datetime.now(timezone.utc)
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        page = conn.execute("SELECT * FROM pages WHERE page_id = ?", (page_id,)).fetchone()
        scope = result.get("rate_limit_scope", "PAGE")
        key = "send_cooldown:APP" if scope == "APP" else (
            f"send_cooldown:ACCOUNT:{page['account_id']}" if scope == "ACCOUNT" and page["account_id"] else None)
        stored = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key = ?", (key,)).fetchone() if key else None
        state = json.loads(stored[0]) if stored else {}
        attempts = (state.get("attempts", 0) if key else page["send_backoff_attempts"]) + 1
        seconds = min(2 ** min(attempts, 16), config.SEND_BACKOFF_MAX_MINUTES) * 60
        seconds = max(seconds, min(float(result.get("retry_after_seconds") or 0), 365 * 86400))
        until = now + timedelta(seconds=seconds)
        previous = state.get("until") if key else page["send_cooldown_until"]
        if previous:
            until = max(until, datetime.fromisoformat(previous))
        value = until.isoformat()
        if key:
            conn.execute("""INSERT INTO app_settings (setting_key,setting_value,updated_at) VALUES (?,?,?)
                ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,updated_at=excluded.updated_at""",
                (key, json.dumps({"until": value, "attempts": attempts}), now.isoformat()))
        where = "1=1" if scope == "APP" else ("account_id = ?" if key else "page_id = ?")
        params = () if scope == "APP" else ((page["account_id"],) if key else (page_id,))
        conn.execute(f"""UPDATE pages SET send_cooldown_until = CASE
            WHEN send_cooldown_until > ? THEN send_cooldown_until ELSE ? END WHERE {where}""",
            (value, value, *params))
        conn.execute("UPDATE pages SET send_backoff_attempts=? WHERE page_id=?", (attempts, page_id))
        return value


def record_comment_success(conn: sqlite3.Connection, page_id: str, when: datetime) -> None:
    """Caller must first atomically transition an unsatisfied operation to success."""
    now = when.isoformat()
    today = local_day_key(when)
    conn.execute("""UPDATE pages SET last_comment_at = ?,
        comments_today_count = CASE WHEN comments_today_date = ? THEN comments_today_count + 1 ELSE 1 END,
        comments_today_date = ?, send_backoff_attempts = 0,
        send_cooldown_until = CASE WHEN send_cooldown_until <= ? THEN NULL ELSE send_cooldown_until END
        WHERE page_id = ?""", (now, today, today, now, page_id))
    account = conn.execute("SELECT account_id FROM pages WHERE page_id=?", (page_id,)).fetchone()
    for key in ("send_cooldown:APP", f"send_cooldown:ACCOUNT:{account[0]}"):
        row = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key=?", (key,)).fetchone()
        if row:
            state = json.loads(row[0])
            if datetime.fromisoformat(state["until"]) <= when:
                state["attempts"] = 0
                conn.execute("UPDATE app_settings SET setting_value=?,updated_at=? WHERE setting_key=?",
                             (json.dumps(state), now, key))


def defer_task(task_id: int, retry_at: Optional[str], message: str) -> None:
    with closing(get_connection()) as conn, conn:
        conn.execute("""UPDATE tasks SET scheduled_at=CASE WHEN ? IS NOT NULL AND scheduled_at < ? THEN ? ELSE scheduled_at END,
            error_message=? WHERE id=? AND status='PENDING'""", (retry_at, retry_at, retry_at, message, task_id))


def refresh_single_task_content(conn, page_id, *, old_delay=None, freeze=False, task_id=None):
    """Refresh unsent single comments within the caller's transaction."""
    row = conn.execute("""SELECT p.page_id,p.page_name,r.templates,r.delay_minutes,r.comment_sequence,r.content_revision
        FROM pages p JOIN page_rules r ON r.page_id=p.page_id WHERE p.page_id=?""", (page_id,)).fetchone()
    if not row:
        return 0
    page = dict(row)
    revision = comment_content.rule_revision(page)
    conn.execute("UPDATE page_rules SET content_revision=? WHERE page_id=?", (revision, page_id))
    freeze = freeze or bool(json.loads(page["comment_sequence"] or "[]"))
    tasks = conn.execute("""SELECT * FROM tasks WHERE page_id=? AND source='AUTO'
        AND sequence_step IS NULL AND status IN ('PENDING','PAUSED') AND (? IS NULL OR id=?)""",
        (page_id, task_id, task_id)).fetchall()
    updated = 0
    for task in tasks:
        if task["content_revision"] and task["content_revision"].startswith("snapshot:"):
            continue
        if freeze:
            if task["template_source"]:
                conn.execute("UPDATE tasks SET content_revision=? WHERE id=?", ("snapshot:" + (task["content_revision"] or revision), task["id"]))
                continue
            material = comment_content.materialize(page, task["post_id"], template=task["comment_content"], revision="snapshot:" + revision)
        else:
            if task["content_revision"] == revision and task["template_source"]:
                continue
            material = comment_content.materialize(page, task["post_id"])
        scheduled = task["scheduled_at"]
        prior_delay = task["content_delay_minutes"] if task["content_delay_minutes"] is not None else old_delay
        if not freeze and prior_delay != page["delay_minutes"] and task["post_created_time"]:
            created = datetime.fromisoformat(task["post_created_time"])
            previous = datetime.fromisoformat(scheduled)
            original = created + timedelta(minutes=prior_delay if prior_delay is not None else page["delay_minutes"])
            target = created + timedelta(minutes=page["delay_minutes"])
            if task["retry_count"] or previous > original + timedelta(seconds=1):
                target = max(target, previous)
            scheduled = target.isoformat()
        conn.execute("""UPDATE tasks SET comment_content=?,template_source=?,content_revision=?,content_delay_minutes=?,scheduled_at=?,updated_at=?
            WHERE id=?""", (material["content"], material["template_source"], material["content_revision"], material["content_delay_minutes"], scheduled,
                            datetime.now(timezone.utc).isoformat(), task["id"]))
        updated += 1
    return updated


def resume_paused_tasks_for_page(page_id: str) -> int:
    """Resume tasks after source preparation, preserving due times and deferrals."""
    conn = get_connection()
    now_iso = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        refresh_single_task_content(conn, page_id)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tasks
            SET status = 'PENDING',
                error_message = NULL,
                updated_at = ?
            WHERE page_id = ? AND status = 'PAUSED'
        """, (now_iso, page_id))
        count = cursor.rowcount
    conn.close()
    return count


def update_page_rule(
    page_id: str,
    delay_minutes: int,
    templates: List[str],
    max_comments_per_day: int = 20,
    is_enabled: Optional[bool] = True,
    conn: Optional[sqlite3.Connection] = None
) -> int:
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    now = datetime.now(timezone.utc).isoformat()
    cancelled_auto = 0
    with (conn if should_close else nullcontext()):
        previous = conn.execute(
            "SELECT is_enabled FROM page_rules WHERE page_id = ?",
            (page_id,),
        ).fetchone()
        was_enabled = bool(previous["is_enabled"]) if previous else False
        conn.execute("""
            INSERT INTO page_rules (page_id, delay_minutes, templates, max_comments_per_day, is_enabled, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(page_id) DO UPDATE SET
                delay_minutes = excluded.delay_minutes,
                templates = excluded.templates,
                max_comments_per_day = excluded.max_comments_per_day,
                is_enabled = CASE WHEN ? IS NULL THEN page_rules.is_enabled ELSE excluded.is_enabled END,
                updated_at = excluded.updated_at
        """, (page_id, delay_minutes, json.dumps(templates, ensure_ascii=False), max_comments_per_day, 1 if is_enabled else 0, now, is_enabled))

        if is_enabled is True and not was_enabled:
            conn.execute(
                "UPDATE pages SET last_seen_post_time = ?, updated_at = ? WHERE page_id = ?",
                (now, now, page_id),
            )
        if is_enabled is False:
            cursor = conn.execute("""
                UPDATE tasks
                SET status = 'CANCELLED', updated_at = ?
                WHERE page_id = ? AND status = 'PENDING' AND source = 'AUTO'
            """, (now, page_id))
            cancelled_auto = cursor.rowcount

    if should_close:
        conn.close()
    return cancelled_auto


def set_page_automation(page_id: str, enabled: bool) -> Dict[str, Any]:
    """Toggles only the per-Page automation permission, independent of Sheet data."""
    now = datetime.now(timezone.utc).isoformat()
    cancelled_auto = 0
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT r.is_enabled FROM page_rules r JOIN pages p ON p.page_id = r.page_id
               WHERE r.page_id = ?""", (page_id,),
        ).fetchone()
        if not row:
            return {"success": False, "error": "Không tìm thấy Page hoặc cấu hình tự động."}
        was_enabled = bool(row["is_enabled"])
        conn.execute(
            "UPDATE page_rules SET is_enabled = ?, updated_at = ? WHERE page_id = ?",
            (1 if enabled else 0, now, page_id),
        )
        if enabled and not was_enabled:
            # Start from now so old posts are not treated as new on first enable/resume.
            conn.execute(
                "UPDATE pages SET last_seen_post_time = ?, updated_at = ? WHERE page_id = ?",
                (now, now, page_id),
            )
        if not enabled:
            cur = conn.execute("""
                UPDATE tasks SET status = 'CANCELLED', updated_at = ?
                WHERE page_id = ? AND status = 'PENDING' AND source = 'AUTO'
            """, (now, page_id))
            cancelled_auto = cur.rowcount
    return {"success": True, "enabled": bool(enabled), "changed": was_enabled != bool(enabled),
            "cancelled_auto_tasks": cancelled_auto}


def update_page_daily_limit(page_id: str, limit: int) -> None:
    with closing(get_connection()) as conn, conn:
        conn.execute("UPDATE page_rules SET max_comments_per_day = ? WHERE page_id = ?", (limit, page_id))


def create_task(
    page_id: str,
    post_id: str,
    comment_content: str,
    scheduled_at: str,
    source: str = "AUTO",
    batch_id: Optional[str] = None,
    post_created_time: Optional[str] = None,
    dedupe_key: Optional[str] = None,
    post_url: Optional[str] = None,
    scan_checkpoint: Optional[str] = None,
    scan_snapshot: Optional[tuple] = None,
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
            if scan_snapshot is not None:
                conn.execute("BEGIN IMMEDIATE")
                ready = conn.execute("""
                    SELECT 1 FROM pages p JOIN page_rules r ON r.page_id = p.page_id
                    WHERE p.page_id = ? AND p.is_active = 1 AND p.last_seen_post_time IS ?
                      AND r.is_enabled = 1 AND r.source_error IS NULL
                      AND r.templates = ? AND r.delay_minutes = ?
                """, (page_id, scan_checkpoint, *scan_snapshot)).fetchone()
                if not ready:
                    return False
            conn.execute("""
                INSERT INTO tasks (page_id, post_id, post_url, dedupe_key, source, batch_id, comment_content, scheduled_at, post_created_time, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """, (page_id, post_id, post_url, dedupe_key, source, batch_id, comment_content, scheduled_at, post_created_time))
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def queue_auto_scan(page: Dict[str, Any], posts: List[Dict[str, Any]], checkpoint: str) -> bool:
    """Commit a scan cursor and its tasks together against the rules used for that scan."""
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(f"""
            UPDATE pages SET last_seen_post_time = ?
            WHERE page_id = ? AND is_active = 1 AND last_seen_post_time IS ?
              AND send_mode = ? AND {_selected_transport_ready_sql('pages')}
              AND (send_mode != 'BROWSER' OR (antidetect_profile_id IS ? AND browser_identity_id IS ?))
              AND EXISTS (
                  SELECT 1 FROM page_rules r WHERE r.page_id = pages.page_id
                    AND r.is_enabled = 1 AND r.source_error IS NULL
                    AND r.templates = ? AND r.delay_minutes = ? AND r.comment_sequence = ?
              )
        """, (
            checkpoint, page["page_id"], page["last_seen_post_time"],
            page.get("send_mode", "GRAPH_API"), config.PILOT_PAGE_ID,
            page.get("antidetect_profile_id"), page.get("browser_identity_id"),
            page["templates"], page["delay_minutes"], page["comment_sequence"],
        ))
        if not updated.rowcount:
            return False

        for post in posts:
            insert_auto_post_tasks(conn, page["page_id"], post)
        return True


def insert_auto_post_tasks(conn: sqlite3.Connection, page_id: str, post: Dict[str, Any]) -> bool:
    """Insert one schedule per published post. Caller owns the write transaction."""
    existing = conn.execute("""
        SELECT 1 FROM tasks WHERE page_id = ? AND post_id = ?
          AND (source = 'AUTO' OR dedupe_key LIKE 'publication:%')
    """, (page_id, post["id"])).fetchone()
    if existing:
        return False
    page = conn.execute("SELECT fb_profile_id FROM pages WHERE page_id = ?", (page_id,)).fetchone()
    owner_ids = (page_id, page["fb_profile_id"] if page else None)
    permalink_key = post_identity.facebook_post_key(post.get("url"), allowed_owner_ids=owner_ids)
    if permalink_key is not None:
        existing_urls = conn.execute("""
            SELECT DISTINCT post_url FROM tasks WHERE page_id = ? AND post_url IS NOT NULL
              AND (source = 'AUTO' OR dedupe_key LIKE 'publication:%')
        """, (page_id,))
        if any(post_identity.facebook_post_key(row["post_url"], allowed_owner_ids=owner_ids) == permalink_key
               for row in existing_urls):
            return False
    pending_publication = conn.execute("""
        SELECT 1 FROM publications WHERE page_id = ? AND status = 'PROCESSING'
          AND first_comment <> '' AND (post_id = ? OR ? = page_id || '_' || post_id)
    """, (page_id, post["id"], post["id"])).fetchone()
    if pending_publication:
        return False
    for step in post["steps"]:
        key = f"auto:{page_id}:{post['id']}"
        if step["number"] is not None:
            key = json.dumps(["sequence", page_id, post["id"], step["number"]])
        conn.execute("""
            INSERT INTO tasks (
                page_id, post_id, post_url, dedupe_key, source, comment_content,
                scheduled_at, post_created_time, sequence_step, sequence_gap_minutes, template_source, content_revision, content_delay_minutes
            ) VALUES (?, ?, ?, ?, 'AUTO', ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            page_id, post["id"], post["url"], key, step["content"],
            step["scheduled_at"], post["created_time"], step["number"], step["gap"],
            step.get("template_source"), step.get("content_revision"),
            step.get("content_delay_minutes"),
        ))
    return True


def get_due_tasks(limit: int = 10, page_id: Optional[str] = None, include_auto: bool = True,
                  exclude_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    now_utc = datetime.now(timezone.utc).isoformat()
    excluded = list(exclude_ids or [])
    exclude_sql = "AND t.id NOT IN (" + ",".join("?" for _ in excluded) + ")" if excluded else ""
    cursor.execute(f"""
        SELECT t.*, p.page_name, p.last_comment_at,
               p.comments_today_count, p.comments_today_date,
               p.connection_mode, p.antidetect_profile_id, p.antidetect_profile_name,
               p.page_url, p.fb_profile_id, p.send_mode, p.browser_status,
               r.max_comments_per_day
        FROM tasks t
        JOIN pages p ON t.page_id = p.page_id
        LEFT JOIN page_rules r ON p.page_id = r.page_id
        WHERE t.status = 'PENDING' AND t.scheduled_at <= ? AND p.is_active = 1
          AND (? IS NULL OR t.page_id = ?)
          AND (t.source != 'AUTO' OR (? AND r.is_enabled = 1 AND (r.source_error IS NULL OR TRIM(r.source_error) = '')))
          AND (p.send_cooldown_until IS NULL OR p.send_cooldown_until <= ?)
          AND {_selected_transport_ready_sql()}
          AND (COALESCE(t.sequence_step, 1) <= 1 OR EXISTS (
              SELECT 1 FROM tasks previous
              WHERE previous.page_id = t.page_id AND previous.post_id = t.post_id
                AND previous.source = 'AUTO' AND previous.sequence_step = t.sequence_step - 1
                AND previous.status = 'SUCCESS'
                AND julianday(previous.updated_at, '+' || t.sequence_gap_minutes || ' minutes') <= julianday(?)
          ))
        {exclude_sql}
        ORDER BY t.scheduled_at ASC, t.id ASC
        LIMIT ?
    """, (now_utc, page_id, page_id, include_auto, now_utc, config.PILOT_PAGE_ID, now_utc, *excluded, limit))
    rows = []
    for r in cursor.fetchall():
        d = dict(r)
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


def claim_due_task(task_id: int, now_iso: str) -> Optional[Dict[str, Any]]:
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        target = conn.execute("SELECT page_id,source,sequence_step,status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if target and target["status"] == "PENDING" and target["source"] == "AUTO" and target["sequence_step"] is None:
            try:
                refresh_single_task_content(conn, target["page_id"], task_id=task_id)
            except ValueError as exc:
                conn.execute("UPDATE page_rules SET source_error=? WHERE page_id=?", (str(exc), target["page_id"]))
                return None
        row = conn.execute(f"""
            SELECT t.* FROM tasks t
            JOIN pages p ON p.page_id = t.page_id
            LEFT JOIN page_rules r ON r.page_id = t.page_id
            WHERE t.id = ? AND t.status = 'PENDING' AND t.scheduled_at <= ?
              AND p.is_active = 1
              AND (p.send_cooldown_until IS NULL OR p.send_cooldown_until <= ?)
              AND {_selected_transport_ready_sql()}
              AND (t.source != 'AUTO' OR (r.is_enabled = 1 AND r.source_error IS NULL))
              AND (COALESCE(t.sequence_step, 1) <= 1 OR EXISTS (
                  SELECT 1 FROM tasks previous
                  WHERE previous.page_id = t.page_id AND previous.post_id = t.post_id
                    AND previous.source = 'AUTO' AND previous.sequence_step = t.sequence_step - 1
                    AND previous.status = 'SUCCESS'
                    AND julianday(previous.updated_at, '+' || t.sequence_gap_minutes || ' minutes') <= julianday(?)
              ))
        """, (task_id, now_iso, now_iso, config.PILOT_PAGE_ID, now_iso)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE tasks SET status = 'SENDING', locked_at = ?, updated_at = ? WHERE id = ?",
                     (now_iso, now_iso, task_id))
        return dict(row)


def release_task_lock(task_id: int) -> bool:
    """Returns a locked-but-not-sent task to PENDING (used when an emergency pause lands)."""
    conn = get_connection()
    with conn:
        cursor = conn.cursor()
        now_iso = datetime.now(timezone.utc).isoformat()
        cursor.execute(f"""
            UPDATE tasks
            SET status = CASE WHEN source = 'AUTO' AND EXISTS (
                    SELECT 1 FROM page_rules WHERE page_id = tasks.page_id AND is_enabled = 0
                 ) THEN 'CANCELLED' WHEN EXISTS (
                     SELECT 1 FROM pages p WHERE p.page_id = tasks.page_id
                       AND (p.is_active != 1
                            OR (p.send_mode='GRAPH_API' AND p.token_status NOT IN ('OK', 'EXPIRING_SOON'))
                            OR (p.send_mode='BROWSER' AND NOT COALESCE({_selected_transport_ready_sql()}, 0)))
                 ) THEN 'PAUSED' ELSE 'PENDING' END, locked_at = NULL, updated_at = ?
            WHERE id = ? AND status = 'SENDING'
        """, (config.PILOT_PAGE_ID, now_iso, task_id))
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
            WHERE id = ? AND (? <> 'SUCCESS' OR status <> 'SUCCESS')
        """, (status, comment_id, error_message, now_utc, status, task_id, status))

        if status == "SUCCESS" and cursor.rowcount:
            advance_sequence(conn, task_id, now_dt)
            cursor.execute("SELECT page_id FROM tasks WHERE id = ?", (task_id,))
            t_row = cursor.fetchone()
            if t_row:
                pid = t_row["page_id"]
                record_comment_success(conn, pid, now_dt)
    conn.close()


def cancel_task(task_id: int) -> bool:
    conn = get_connection()
    with conn:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cursor.execute("""
            UPDATE tasks SET status = 'CANCELLED', updated_at = ?
            WHERE id = ? AND status IN ('PENDING', 'PAUSED')
        """, (now, task_id))
        success = cursor.rowcount > 0
        if success:
            conn.execute("""
                UPDATE tasks SET status = 'CANCELLED', updated_at = ?
                WHERE source = 'AUTO' AND status IN ('PENDING', 'PAUSED')
                  AND (page_id, post_id) = (SELECT page_id, post_id FROM tasks WHERE id = ?)
                  AND sequence_step > (SELECT sequence_step FROM tasks WHERE id = ?)
            """, (now, task_id, task_id))
    conn.close()
    return success


def retry_task(task_id: int, confirmed_not_posted: bool = False) -> bool:
    """
    Safely resets a task back to PENDING only if it is currently in
    FAILED or PAUSED status, or NEED_CHECK after explicit confirmation.
    Never retries a task that is already SUCCESS or SENDING!
    """
    conn = get_connection()
    now_iso = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tasks
            SET status = 'PENDING',
                scheduled_at = CASE WHEN status = 'PAUSED' OR scheduled_at > ? THEN scheduled_at ELSE ? END,
                retry_count = 0,
                locked_at = NULL,
                error_message = NULL,
                updated_at = ?
            WHERE id = ? AND (status IN ('FAILED', 'PAUSED') OR (status = 'NEED_CHECK' AND ?))
        """, (now_iso, now_iso, now_iso, task_id, confirmed_not_posted))
        success = cursor.rowcount > 0
        if success:
            target = conn.execute("SELECT page_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            refresh_single_task_content(conn, target["page_id"], task_id=task_id)
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
            advance_sequence(conn, task_id, datetime.fromisoformat(now_iso))
            cursor.execute("SELECT page_id FROM tasks WHERE id = ?", (task_id,))
            t_row = cursor.fetchone()
            if t_row:
                record_comment_success(conn, t_row["page_id"], datetime.fromisoformat(now_iso))
    conn.close()
    return success


def advance_sequence(conn: sqlite3.Connection, task_id: int, sent_at: datetime) -> None:
    row = conn.execute("""
        SELECT next.id, next.sequence_gap_minutes, next.scheduled_at
        FROM tasks previous
        JOIN tasks next ON next.page_id = previous.page_id AND next.post_id = previous.post_id
                       AND next.sequence_step = previous.sequence_step + 1
        WHERE previous.id = ? AND next.status IN ('PENDING', 'PAUSED')
    """, (task_id,)).fetchone()
    if row:
        earliest = sent_at + timedelta(minutes=row["sequence_gap_minutes"])
        if earliest > datetime.fromisoformat(row["scheduled_at"]):
            conn.execute("UPDATE tasks SET scheduled_at = ? WHERE id = ?", (earliest.isoformat(), row["id"]))


def get_all_tasks(page_id: Optional[str] = None, status: Optional[str] = None, source: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    query = """
        SELECT t.id, t.page_id, t.post_id, t.post_url, t.dedupe_key, t.source, t.batch_id,
               t.sequence_step, t.sequence_gap_minutes, t.template_source, t.content_revision,
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
