"""Shared dispatch policy for post comments and inbox replies.

Acquire the Page guard before claiming a write. Keep it until the result and
counter are committed. No network operation holds a SQLite transaction.
"""
from contextlib import contextmanager
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading
import time

import config
import database as db
import browser_safety
import submit_control

_locks = {}
_locks_lock = threading.Lock()
_graph_quarantine_lock = threading.Lock()
_graph_quarantine = set()


def _graph_key(page_id):
    return str(db.DB_PATH.resolve()), str(page_id)


def quarantine_graph(page_id):
    """Stop Graph admissions before attempting to persist a restriction."""
    with submit_control.mutation(page_id):
        with _graph_quarantine_lock:
            _graph_quarantine.add(_graph_key(page_id))


def clear_graph_quarantine(page_id):
    """Called only after a new successful connection proof is committed."""
    with _graph_quarantine_lock:
        _graph_quarantine.discard(_graph_key(page_id))
    submit_control.invalidate_page(page_id)


def graph_quarantined(page_id):
    with _graph_quarantine_lock:
        return _graph_key(page_id) in _graph_quarantine


def page_lock(page_id):
    with _locks_lock:
        return _locks.setdefault(page_id, threading.Lock())


def wait_until_idle():
    with _locks_lock:
        locks = list(_locks.values())
    for lock in locks:
        with lock:
            pass


def graph_ready(page):
    return bool(page and page.get("is_active") and page.get("page_access_token")
                and not graph_quarantined(page.get("page_id"))
                and page.get("token_status") in ("OK", "EXPIRING_SOON")
                and page.get("connection_checked_at") and not page.get("connection_error"))


def browser_ready(page, *, safety=None):
    return bool(page and page.get("is_active") and page.get("send_mode") == "BROWSER"
                and page.get("browser_status") == "READY" and page.get("browser_checked_at")
                and not page.get("browser_error") and page.get("antidetect_profile_id")
                and page.get("browser_profile_id") == page.get("antidetect_profile_id")
                and page.get("browser_identity_id")
                and page.get("browser_identity_id") == db.browser_identity_id(page)
                and (safety if safety is not None else browser_safety.status(page["antidetect_profile_id"]))["browser_status"] == "READY")


def send_ready(page, *, safety=None):
    if page and page.get("send_mode", "GRAPH_API") == "BROWSER":
        return browser_ready(page, safety=safety)
    return bool(page and page.get("send_mode", "GRAPH_API") == "GRAPH_API" and graph_ready(page))


@dataclass
class Decision:
    reason: str
    page: dict = None
    retry_at: str = None
    message: str = ""

    @property
    def ready(self):
        return self.reason == "READY"

    def public(self):
        return {"success": False, "waiting": True, "reason": self.reason,
                "retry_at": self.retry_at, "message": self.message}


def check(page_id, paused, *, automatic=False, require_sheet=False, transport="GRAPH_API", profile_reserved=False, _policy=None):
    if paused():
        return Decision("SYSTEM_PAUSED", message="Hệ thống đang tạm dừng gửi bình luận.")
    page = _policy["page"] if _policy is not None else db.get_page_for_dispatch(page_id, transport=transport)
    safety = _policy.get("safety") if _policy is not None else None
    if not (send_ready(page, safety=safety) if transport == "selected" else graph_ready(page)):
        browser = transport == "selected" and page and page.get("send_mode") == "BROWSER"
        message = (page.get("browser_error") or "Page cần kiểm tra kết nối trình duyệt.") if browser else "Page cần kết nối Meta hợp lệ."
        return Decision("PAGE_BLOCKED", page, message=message)
    if automatic and (not page.get("rule_enabled") or (require_sheet and page.get("source_error"))):
        return Decision("AUTOMATION_PAUSED", page, message="Tự động của Page đang tạm dừng hoặc cấu hình chưa hợp lệ.")
    now = datetime.now(timezone.utc)
    waits = []
    if transport == "selected" and page.get("send_mode") == "BROWSER":
        safety = safety if safety is not None else browser_safety.status(page["antidetect_profile_id"])
        if not safety["ready"]:
            if not safety["retry_at"]:
                return Decision(safety["reason"], page, message=safety["message"])
            if not profile_reserved:
                waits.append((safety["retry_at"], safety["reason"], safety["message"]))
    cooldown = _policy["cooldown"] if _policy is not None else db.get_send_cooldown(page, now)
    if cooldown:
        waits.append((cooldown, "WAIT_RATE_LIMIT", "Page đang chờ giới hạn gửi."))
    limit = page.get("max_comments_per_day") or config.MAX_COMMENTS_PER_PAGE_PER_DAY
    if page.get("comments_today_date") == db.local_day_key(now) and (page.get("comments_today_count") or 0) >= limit:
        waits.append((db.next_daily_reset_utc(now), "WAIT_DAILY", "Đã hết hạn mức bình luận hôm nay."))
    if page.get("last_comment_at"):
        last = datetime.fromisoformat(page["last_comment_at"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        earliest = last + timedelta(seconds=config.MIN_GAP_PER_PAGE_SEC)
        if earliest > now:
            waits.append((earliest.isoformat(), "WAIT_GAP", "Đang chờ khoảng cách giữa các bình luận."))
    if waits:
        retry_at, reason, message = max(waits, key=lambda item: datetime.fromisoformat(item[0]))
        return Decision(reason, page, retry_at, message)
    return Decision("READY", page)


FINAL_POLICY_TIMEOUT_SEC = 0.2


def final_check(page_id, paused, initial_page, *, automatic=False, require_sheet=False,
                transport="selected", profile_reserved=False, task=None):
    """Read the final Page/task/quota policy in one short, read-only snapshot.

    No token is decrypted here. Graph binding is compared inside SQL and the
    already prepared runtime token is used only if that binding is unchanged.
    Caller must subsequently commit its admission ticket without more I/O.
    """
    started = time.monotonic()
    if paused():
        return Decision("SYSTEM_PAUSED", message="Hệ thống đang tạm dừng gửi.")
    try:
        with closing(sqlite3.connect(db.DB_PATH, timeout=FINAL_POLICY_TIMEOUT_SEC)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            row = conn.execute("""
                SELECT p.page_id,p.account_id,p.is_active,p.token_status,p.send_mode,
                       p.connection_checked_at,p.connection_error,p.antidetect_profile_id,
                       p.fb_profile_id,p.browser_identity_id,p.browser_profile_id,p.browser_status,
                       p.browser_checked_at,p.browser_error,p.comments_today_date,p.comments_today_count,
                       p.last_comment_at,p.send_cooldown_until,p.page_name,
                       r.is_enabled AS rule_enabled,r.source_error,r.delay_minutes,r.templates,
                       r.comment_sequence,r.content_revision,r.max_comments_per_day,
                       (p.page_access_token_enc=?) AS same_token
                FROM pages p LEFT JOIN page_rules r ON r.page_id=p.page_id WHERE p.page_id=?
            """, (initial_page.get("page_access_token_enc", ""), page_id)).fetchone()
            if row is None:
                return Decision("PAGE_CHANGED", message="Page không còn khả dụng.")
            page = dict(row)
            browser = transport == "selected" and page["send_mode"] == "BROWSER"
            if browser:
                keys = ("antidetect_profile_id", "fb_profile_id", "browser_identity_id", "browser_profile_id")
                if any(page.get(key) != initial_page.get(key) for key in keys):
                    return Decision("PAGE_CHANGED", message="Liên kết Browser đã thay đổi.")
                safety = browser_safety.status(page["antidetect_profile_id"], conn=conn)
                page["page_access_token"] = ""
            else:
                if not page.pop("same_token") or page["account_id"] != initial_page.get("account_id"):
                    return Decision("PAGE_CHANGED", message="Kết nối Meta đã thay đổi.")
                page["page_access_token"] = initial_page.get("page_access_token", "")
                safety = None
            if transport == "selected" and page["send_mode"] != initial_page.get("send_mode", "GRAPH_API"):
                return Decision("PAGE_CHANGED", message="Cách gửi của Page đã thay đổi.")
            deadlines = [page.get("send_cooldown_until")]
            for key in ("send_cooldown:APP", f"send_cooldown:ACCOUNT:{page['account_id']}"):
                stored = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key=?", (key,)).fetchone()
                if stored:
                    deadlines.append(json.loads(stored[0]).get("until"))
            now = datetime.now(timezone.utc)
            deadlines = [value for value in deadlines if value and datetime.fromisoformat(value) > now]
            cooldown = max(deadlines, key=datetime.fromisoformat) if deadlines else None
            if task is not None:
                current = conn.execute("SELECT status,comment_content FROM tasks WHERE id=? AND page_id=?",
                                       (task["id"], page_id)).fetchone()
                if not current or current["status"] != "SENDING" or current["comment_content"] != task["comment_content"]:
                    return Decision("TASK_CHANGED", message="Tác vụ đã thay đổi trước khi gửi.")
            result = check(page_id, paused, automatic=automatic, require_sheet=require_sheet,
                           transport=transport, profile_reserved=profile_reserved,
                           _policy={"page": page, "safety": safety, "cooldown": cooldown})
        if time.monotonic() - started > FINAL_POLICY_TIMEOUT_SEC:
            return Decision("POLICY_UNAVAILABLE", message="Kiểm tra chính sách quá chậm; đã hủy lượt gửi.")
        return result
    except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
        return Decision("POLICY_UNAVAILABLE", message="Chưa xác minh được chính sách gửi; đã hủy lượt gửi.")


def page_status(page, paused=False, *, safety=None):
    """Read-only status projection, including shared cooldowns and local-day counts."""
    now = datetime.now(timezone.utc)
    used = (page.get("comments_today_count") or 0) if page.get("comments_today_date") == db.local_day_key(now) else 0
    limit = page.get("max_comments_per_day") or config.MAX_COMMENTS_PER_PAGE_PER_DAY
    deadlines = []
    cooldown = db.get_send_cooldown(page, now)
    if cooldown:
        deadlines.append((cooldown, "Đang chờ giới hạn gửi"))
    if used >= limit:
        deadlines.append((db.next_daily_reset_utc(now), "Đã hết hạn mức hôm nay"))
    if page.get("last_comment_at"):
        last = datetime.fromisoformat(page["last_comment_at"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        earliest = last + timedelta(seconds=config.MIN_GAP_PER_PAGE_SEC)
        if earliest > now:
            deadlines.append((earliest.isoformat(), "Đang chờ khoảng cách gửi"))
    until, reason = max(deadlines, key=lambda item: datetime.fromisoformat(item[0])) if deadlines else (None, None)
    if paused:
        reason = "Hệ thống đang tạm dừng"
    elif page.get("send_mode") == "BROWSER" and not browser_ready(page, safety=safety):
        reason = page.get("browser_error") or "Page cần kiểm tra kết nối trình duyệt"
    elif page.get("send_mode", "GRAPH_API") == "GRAPH_API" and (not page.get("is_active") or page.get("token_status") not in ("OK", "EXPIRING_SOON")):
        reason = page.get("status_note") or "Page cần kết nối Meta hợp lệ"
    profile = {}
    if page.get("send_mode") == "BROWSER" and page.get("antidetect_profile_id"):
        safety = safety if safety is not None else browser_safety.status(page["antidetect_profile_id"])
        profile = {"profile_operations_today": safety["count"],
                   "profile_operations_limit": safety.get("max_operations_per_day"),
                   "profile_wait_reason": safety["message"] if not safety["ready"] else None,
                   "profile_retry_at": safety["retry_at"]}
        if not paused and not safety["ready"]:
            if not safety["retry_at"] or not until or datetime.fromisoformat(safety["retry_at"]) >= datetime.fromisoformat(until):
                reason, until = safety["message"], safety["retry_at"]
    return {"comments_used_today": used, "comments_remaining_today": max(0, limit - used),
            "send_wait_reason": reason, "next_send_at": until, **profile}


@contextmanager
def guard(page_id, paused, *, automatic=False, require_sheet=False, transport="GRAPH_API"):
    lock = page_lock(page_id)
    if not lock.acquire(blocking=False):
        yield Decision("WAIT_BUSY", retry_at=(datetime.now(timezone.utc) + timedelta(seconds=config.TASK_TICK_SEC)).isoformat(),
                       message="Page đang xử lý một bình luận khác.")
        return
    try:
        yield check(page_id, paused, automatic=automatic, require_sheet=require_sheet, transport=transport)
    finally:
        lock.release()
