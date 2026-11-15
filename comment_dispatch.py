"""Shared dispatch policy for post comments and inbox replies.

Acquire the Page guard before claiming a write. Keep it until the result and
counter are committed. No network operation holds a SQLite transaction.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import threading

import config
import database as db

_locks = {}
_locks_lock = threading.Lock()


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
                and page.get("token_status") in ("OK", "EXPIRING_SOON")
                and (page.get("page_id") == config.PILOT_PAGE_ID
                     or (page.get("connection_checked_at") and not page.get("connection_error"))))


def browser_ready(page):
    return bool(page and page.get("is_active") and page.get("send_mode") == "BROWSER"
                and page.get("browser_status") == "READY" and page.get("browser_checked_at")
                and not page.get("browser_error") and page.get("antidetect_profile_id")
                and page.get("browser_profile_id") == page.get("antidetect_profile_id")
                and page.get("browser_identity_id")
                and page.get("browser_identity_id") == db.browser_identity_id(page))


def send_ready(page):
    if page and page.get("send_mode", "GRAPH_API") == "BROWSER":
        return browser_ready(page)
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


def check(page_id, paused, *, automatic=False, require_sheet=False, transport="GRAPH_API"):
    if paused():
        return Decision("SYSTEM_PAUSED", message="Hệ thống đang tạm dừng gửi bình luận.")
    page = db.get_page_for_dispatch(page_id, transport=transport)
    if not (send_ready(page) if transport == "selected" else graph_ready(page)):
        browser = transport == "selected" and page and page.get("send_mode") == "BROWSER"
        message = (page.get("browser_error") or "Page cần kiểm tra kết nối trình duyệt.") if browser else "Page cần kết nối Meta hợp lệ."
        return Decision("PAGE_BLOCKED", page, message=message)
    if automatic and (not page.get("rule_enabled") or (require_sheet and page.get("source_error"))):
        return Decision("AUTOMATION_PAUSED", page, message="Tự động của Page đang tạm dừng hoặc cấu hình chưa hợp lệ.")
    now = datetime.now(timezone.utc)
    waits = []
    cooldown = db.get_send_cooldown(page, now)
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


def page_status(page, paused=False):
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
    elif page.get("send_mode") == "BROWSER" and not browser_ready(page):
        reason = page.get("browser_error") or "Page cần kiểm tra kết nối trình duyệt"
    elif page.get("send_mode", "GRAPH_API") == "GRAPH_API" and (not page.get("is_active") or page.get("token_status") not in ("OK", "EXPIRING_SOON")):
        reason = page.get("status_note") or "Page cần kết nối Meta hợp lệ"
    return {"comments_used_today": used, "comments_remaining_today": max(0, limit - used),
            "send_wait_reason": reason, "next_send_at": until}


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
