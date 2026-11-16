import json
import asyncio
import threading
import time
from contextlib import closing
from datetime import datetime, timezone, timedelta
from dateutil import parser as date_parser
from typing import Dict, Any, Optional

import config
import comment_dispatch
import comment_content
import database as db
import meta_client
import sheets_sync
import antidetect_client
import browser_safety


# In-memory status for UI monitoring
SCHEDULER_METRICS: Dict[str, Any] = {
    "post_scan": {"last_run": None, "last_error": None, "status": "RUNNING"},
    "task_exec": {"last_run": None, "last_error": None, "status": "RUNNING"},
    "maintenance": {"last_run": None, "last_error": None, "status": "RUNNING"},
    "sheet_sync": {"last_run": None, "last_error": None, "sheet_url": config.PILOT_SHEET_URL or config.DEFAULT_SHEET_URL,
                   "source": "LEGACY_PILOT" if config.PILOT_SHEET_URL else "DASHBOARD"},
    "paused_all": True,
    "scan_errors": {},
    "send_errors": {},


}


_SCAN_LOCK = threading.Lock()
_EXEC_LOCK = threading.Lock()
_RUN_STATE_LOCK = threading.Lock()
_COMMENT_SHEET_LOCK = threading.Lock()
_last_comment_sheet_read = float("-inf")
_comment_sheet_result = {"success": False, "error": "Chưa đọc tab bình luận."}


def set_system_paused(paused: bool) -> None:
    with _RUN_STATE_LOCK:
        if paused:
            SCHEDULER_METRICS["paused_all"] = True
        db.set_setting("automation_run_state", "paused" if paused else "running")
        SCHEDULER_METRICS["paused_all"] = paused


def comment_sheet_url() -> str:
    """A dashboard-selected tab takes precedence; the legacy pilot URL is a safe fallback."""
    try:
        saved = db.get_setting("comment_sheet_url", "").strip()
    except Exception:
        saved = ""
    return saved or config.MULTI_PAGE_SHEET_URL or config.PILOT_SHEET_URL or config.DEFAULT_SHEET_URL


def refresh_comment_sheet(force=False, candidate_url=None):
    global _last_comment_sheet_read, _comment_sheet_result
    with _COMMENT_SHEET_LOCK:
        url = candidate_url if candidate_url is not None else comment_sheet_url()
        if not url:
            return {"success": False, "error": "Chưa cấu hình tab bình luận multi-Page."}
        status = SCHEDULER_METRICS["sheet_sync"]
        if force or url != status["sheet_url"] or time.monotonic() - _last_comment_sheet_read >= 30:
            try:
                result = sheets_sync.sync_comment_sheet(
                    url, legacy_page_id=config.PILOT_PAGE_ID, save_url=candidate_url is not None,
                )
            except Exception:
                result = {"success": False, "error": "Không thể cập nhật mẫu bình luận trong DB."}
            if candidate_url is not None and not result["success"] and url != comment_sheet_url():
                return result
            _comment_sheet_result = result
            _last_comment_sheet_read = time.monotonic()
            status["sheet_url"] = url
            status["last_run"] = datetime.now(timezone.utc).isoformat()
            status["last_error"] = _comment_sheet_result.get("error") or (
                f"{len(_comment_sheet_result.get('errors') or [])} cảnh báo cấu hình theo Page."
                if _comment_sheet_result.get("errors") else None
            )
            if _comment_sheet_result.get("success"):
                status["templates_count"] = _comment_sheet_result.get("templates_count", 0)
                status["pages_synced"] = _comment_sheet_result.get("pages_synced", 0)
                status["page_errors"] = _comment_sheet_result.get("errors", [])
                status["sheet_mode"] = _comment_sheet_result.get("sheet_mode")
        return _comment_sheet_result


_SCAN_BACKOFF_UNTIL = {}


def graph_ready(page: Dict[str, Any]) -> bool:
    return comment_dispatch.graph_ready(page)


def send_ready(page: Dict[str, Any]) -> bool:
    return comment_dispatch.send_ready(page)


def _send_mode(page):
    return (page or {}).get("send_mode") or "GRAPH_API"


def _same_send_connection(before, after):
    """Never reuse a verification after the operator changes its connection."""
    if not after or _send_mode(before) != _send_mode(after):
        return False
    keys = ("antidetect_profile_id", "fb_profile_id", "browser_identity_id", "browser_profile_id") \
        if _send_mode(before) == "BROWSER" else ("page_access_token",)
    return all(before.get(key) == after.get(key) for key in keys)


def _record_browser_connection_failure(page, result):
    """Invalidate only the browser binding used by this operation, never its token."""
    category = result.get("category")
    status = result.get("browser_status")
    if status not in ("LOGIN_REQUIRED", "CHECKPOINT", "RESTRICTED", "ERROR"):
        return
    if category in ("BROWSER_BUSY", "BROWSER_LIMIT", "BROWSER_BLOCKED", "CANCELLED_BEFORE_SEND", "NETWORK_RETRYABLE", "RATE_LIMIT", "POST_ERROR"):
        return
    if status in ("LOGIN_REQUIRED", "CHECKPOINT", "RESTRICTED"):
        if not result.get("profile_safety_recorded") and page.get("antidetect_profile_id"):
            browser_safety.block(page["antidetect_profile_id"], status, result.get("error"))
        return
    db.invalidate_page_browser(
        page["page_id"], status, result.get("error"),
        profile_id=page.get("antidetect_profile_id"),
        identity_id=page.get("browser_identity_id"),
    )


def build_post_schedule(page: Dict[str, Any], post_id: str, post_url: Optional[str],
                        created_at: datetime, now: datetime) -> Dict[str, Any]:
    delay_minutes = page["delay_minutes"]
    first_due = max(now, created_at + timedelta(minutes=delay_minutes))
    sequence = json.loads(page["comment_sequence"] or "[]")
    steps = []
    previous_delay = delay_minutes
    for number, step in enumerate(sequence, start=1):
        delay = step["delay_minutes"]
        steps.append({
            "number": number, "gap": delay - previous_delay,
            **comment_content.materialize(page, post_id, template=step["template"], step=number),
            "scheduled_at": (first_due + timedelta(minutes=delay - delay_minutes)).isoformat(),
        })
        previous_delay = delay
    if not sequence:
        steps.append({
            "number": None, "gap": None,
            **comment_content.materialize(page, post_id),
            "scheduled_at": first_due.isoformat(),
        })
    return {"id": post_id, "url": post_url, "created_time": created_at.isoformat(), "steps": steps}


def detect_new_posts_and_schedule():
    """Single-flight wrapper: a background loop and a manual sync must not scan concurrently."""
    if not _SCAN_LOCK.acquire(blocking=False):
        return
    try:
        _detect_new_posts_and_schedule()
    finally:
        _SCAN_LOCK.release()


_BROWSER_SCAN_ATTEMPT_PREFIX = "browser_scan_attempt:"


def _fair_scan_order(pages):
    """Rotate each profile's Browser pages without changing Graph page slots."""
    with closing(db.get_connection()) as conn:
        attempts = dict(conn.execute(
            "SELECT setting_key,setting_value FROM app_settings WHERE setting_key LIKE ?",
            (_BROWSER_SCAN_ATTEMPT_PREFIX + "%",),
        ))

    def last_attempt(page):
        raw = attempts.get(_BROWSER_SCAN_ATTEMPT_PREFIX + page["page_id"])
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
        return datetime.min.replace(tzinfo=timezone.utc)

    profiles = {}
    for page in pages:
        if _send_mode(page) == "BROWSER":
            profiles.setdefault(page.get("antidetect_profile_id"), []).append(page)
    ordered = {profile: iter(sorted(group, key=last_attempt)) for profile, group in profiles.items()}
    return [next(ordered[page.get("antidetect_profile_id")]) if _send_mode(page) == "BROWSER" else page
            for page in pages]


def _scan_browser_posts(page, since):
    # A denied reservation did not use this Page's turn. Empty scans and failed
    # browser attempts did, and must rotate even when the post cursor stays put.
    attempted = True
    try:
        return antidetect_client.get_recent_posts_for_page_cdp(page, since_timestamp=since)
    except antidetect_client.BrowserAutomationError as exc:
        attempted = exc.category not in ("BROWSER_BUSY", "BROWSER_LIMIT", "BROWSER_BLOCKED")
        raise
    finally:
        if attempted:
            db.set_setting(_BROWSER_SCAN_ATTEMPT_PREFIX + page["page_id"], datetime.now(timezone.utc).isoformat())


def _detect_new_posts_and_schedule():
    """
    Scans all active pages incrementally, detecting newly published posts.
    Calculates target delay and creates deduplicated auto-tasks.
    """
    if SCHEDULER_METRICS["paused_all"]:
        return
    if not refresh_comment_sheet()["success"]:
        return
    pages = _fair_scan_order(db.get_all_pages())
    now_utc = datetime.now(timezone.utc)

    for p in pages:
        if SCHEDULER_METRICS["paused_all"]:
            break
        page_id = p["page_id"]
        if not p.get("is_active") or not p.get("rule_enabled") or p.get("source_error"):
            continue
        if _SCAN_BACKOFF_UNTIL.get(page_id, now_utc) > now_utc:
            continue
        try:
            page_full = db.get_page_for_dispatch(page_id)
            if not page_full or not send_ready(page_full):
                continue
            templates = json.loads(p.get("templates") or "[]")
            if not templates:
                continue
            since = p.get("last_seen_post_time") or now_utc.isoformat()
            checkpoint = date_parser.isoparse(since)
            if checkpoint.tzinfo is None:
                checkpoint = checkpoint.replace(tzinfo=timezone.utc)
            if not p.get("last_seen_post_time"):
                with db.get_connection() as conn:
                    conn.execute("UPDATE pages SET last_seen_post_time = ? WHERE page_id = ? AND last_seen_post_time IS NULL",
                                 (since, page_id))
                continue
            if _send_mode(page_full) == "BROWSER":
                posts = _scan_browser_posts(page_full, since)
            else:
                posts = meta_client.get_recent_posts(page_id, page_full["page_access_token"], since_timestamp=since)
        except antidetect_client.BrowserAutomationError as exc:
            SCHEDULER_METRICS["scan_errors"][page_id] = exc.message
            _record_browser_connection_failure(page_full, exc.as_result())
            if exc.category in ("BROWSER_BUSY", "BROWSER_LIMIT", "NETWORK_RETRYABLE"):
                retry_at = exc.as_result().get("retry_at")
                _SCAN_BACKOFF_UNTIL[page_id] = date_parser.isoparse(retry_at) if retry_at else now_utc + timedelta(seconds=config.TASK_TICK_SEC)
            continue
        except meta_client.MetaAPIError as exc:
            SCHEDULER_METRICS["scan_errors"][page_id] = exc.message
            if exc.category == "RATE_LIMIT":
                _SCAN_BACKOFF_UNTIL[page_id] = now_utc + timedelta(minutes=15)
            elif exc.category in ("TOKEN_EXPIRED", "MISSING_PERMISSIONS"):
                status = "EXPIRED" if exc.category == "TOKEN_EXPIRED" else exc.category
                db.update_page_status(page_id, bool(p["is_active"]), status, exc.message)
            continue
        except Exception:
            SCHEDULER_METRICS["scan_errors"][page_id] = "Không thể đọc bài hoặc cấu hình Page."
            continue

        SCHEDULER_METRICS["scan_errors"].pop(page_id, None)
        if SCHEDULER_METRICS["paused_all"]:
            break
        latest_page = db.get_page_for_dispatch(page_id)
        if not send_ready(latest_page) or not _same_send_connection(page_full, latest_page):
            continue
        latest_post_time = None
        new_posts = []
        for post in posts:
            if SCHEDULER_METRICS["paused_all"]:
                return
            post_id = post.get("id")
            created_str = post.get("created_time")
            post_url = post.get("permalink_url")
            if not post_id or not created_str:
                continue

            try:
                created_dt = date_parser.isoparse(created_str)
                if created_dt.tzinfo is None:
                    continue
                created_dt = created_dt.astimezone(timezone.utc)
            except Exception:
                continue

            if created_dt <= checkpoint or created_dt > now_utc:
                continue
            if latest_post_time is None or created_dt > latest_post_time:
                latest_post_time = created_dt

            # Skip posts that are older than MAX_POST_AGE_MIN
            age_minutes = (now_utc - created_dt).total_seconds() / 60.0
            if age_minutes > config.MAX_POST_AGE_MIN:
                continue

            try:
                new_posts.append(build_post_schedule(p, post_id, post_url, created_dt, now_utc))
            except ValueError as exc:
                with db.get_connection() as conn:
                    conn.execute("UPDATE page_rules SET source_error=? WHERE page_id=?", (str(exc), page_id))
                SCHEDULER_METRICS["scan_errors"][page_id] = str(exc)
                break

        if latest_post_time:
            if SCHEDULER_METRICS["paused_all"]:
                return
            db.queue_auto_scan(p, new_posts, latest_post_time.isoformat())


def execute_due_tasks():
    """Single-flight wrapper: manual sync and the background loop must not execute concurrently."""
    if not _EXEC_LOCK.acquire(blocking=False):
        return
    try:
        _execute_due_tasks()
    finally:
        _EXEC_LOCK.release()


def _due_task_candidates(include_auto):
    seen = []
    for _ in range(10):
        batch = db.get_due_tasks(limit=10, include_auto=include_auto, exclude_ids=seen)
        if not batch:
            break
        for task in batch:
            seen.append(task["id"])
            yield task


def _prepare_before_submit(task, initial_page, paused, *, refresh_sheet=False, profile_reserved=False):
    """Recheck policy after slow browser navigation without acquiring the Page lock again."""
    automatic = task.get("source") == "AUTO"
    if automatic and refresh_sheet and not refresh_comment_sheet(force=True).get("success"):
        return comment_dispatch.Decision("SOURCE_CHANGED", message="Chưa đọc được Sheet mới nhất trước khi gửi.")
    with _COMMENT_SHEET_LOCK:
        if automatic and not _comment_sheet_result.get("success"):
            return comment_dispatch.Decision("SOURCE_CHANGED", message="Nguồn bình luận chưa sẵn sàng.")
        latest = comment_dispatch.check(task["page_id"], paused, automatic=automatic,
                                        require_sheet=automatic, transport="selected", profile_reserved=profile_reserved)
        if not latest.ready:
            return latest
        if not _same_send_connection(initial_page, latest.page):
            return comment_dispatch.Decision("PAGE_CHANGED", message="Kết nối Page đã thay đổi trước khi gửi.")
        with closing(db.get_connection()) as conn:
            current_task = conn.execute("SELECT status,comment_content FROM tasks WHERE id=?", (task["id"],)).fetchone()
        if not current_task or current_task["status"] != "SENDING" or current_task["comment_content"] != task["comment_content"]:
            return comment_dispatch.Decision("TASK_CHANGED", message="Tác vụ đã thay đổi trước khi gửi.")
        revision = task.get("content_revision") or ""
        # Graph requests retain their claimed snapshot. Browser navigation can
        # take much longer, so reprepare unsent single comments if its rules changed.
        if _send_mode(initial_page) == "BROWSER" and automatic and task.get("sequence_step") is None and not revision.startswith("snapshot:"):
            if revision != comment_content.rule_revision(latest.page):
                return comment_dispatch.Decision("SOURCE_CHANGED", message="Mẫu hoặc lịch bình luận đã đổi; sẽ chuẩn bị lại tác vụ.")
        return latest


def _execute_due_tasks():
    if SCHEDULER_METRICS["paused_all"]:
        return
    sheet_ready = refresh_comment_sheet()["success"] if comment_sheet_url() else False
    attempts = 0
    paused = lambda: SCHEDULER_METRICS["paused_all"]
    for task in _due_task_candidates(include_auto=bool(sheet_ready)):
        if paused() or attempts >= 10:
            break
        task_id, page_id = task["id"], task["page_id"]
        automatic = task.get("source") == "AUTO"
        with comment_dispatch.guard(page_id, paused, automatic=automatic, require_sheet=automatic,
                                    transport="selected") as decision:
            if not decision.ready:
                db.defer_task(task_id, decision.retry_at, decision.message)
                continue
            page = decision.page
            browser = _send_mode(page) == "BROWSER"
            token = page.get("page_access_token")
            with _COMMENT_SHEET_LOCK:
                if automatic and not _comment_sheet_result.get("success"):
                    continue
                claimed = db.claim_due_task(task_id, datetime.now(timezone.utc).isoformat())
            if not claimed:
                continue
            task = claimed
            content = task["comment_content"]
            due = datetime.fromisoformat(task["scheduled_at"])
            if (datetime.now(timezone.utc) - due).total_seconds() > 7200:
                db.update_task_result(task_id, "NEED_CHECK", error_message="Tác vụ quá hạn hơn 2 giờ. Đối chiếu bài trước khi gửi lại.")
                continue
            if not content.strip():
                db.update_task_result(task_id, "FAILED", error_message="Nội dung bình luận trống.")
                continue
            if paused():
                db.release_task_lock(task_id)
                break
            attempts += 1
            try:
                if not browser:
                    verified = meta_client.verify_post_belongs_to_page(task["post_id"], page_id, token)
            except (meta_client.MetaAPIError, antidetect_client.BrowserAutomationError) as exc:
                result = exc.as_result()
            except Exception:
                result = {"success": False, "category": "NETWORK_RETRYABLE", "error": "Không xác minh được bài trước khi gửi."}
            else:
                latest = _prepare_before_submit(task, page, paused)
                if not latest.ready:
                    db.release_task_lock(task_id)
                    db.defer_task(task_id, latest.retry_at, latest.message)
                    continue
                try:
                    if browser:
                        cancellation = {}

                        def before_submit():
                            final = _prepare_before_submit(task, page, paused, refresh_sheet=True, profile_reserved=True)
                            if not final.ready:
                                cancellation.update(error=final.message, retry_at=final.retry_at)
                            return final.ready

                        # The browser sender verifies the post and its timestamp
                        # in the same tab it uses to submit the comment.
                        result = antidetect_client.send_page_comment_cdp(
                            page, task["post_id"], content,
                            post_url=task.get("post_url"),
                            before_submit=before_submit,
                        )
                        if result.get("category") == "CANCELLED_BEFORE_SEND":
                            result.update(cancellation)
                        if result.get("success") and not result.get("comment_id"):
                            result = {"success": False, "category": "NEED_CHECK",
                                      "error": "Chưa có mã bình luận xác thực từ trình duyệt. Đối chiếu trên Facebook."}
                    else:
                        result = meta_client.post_comment(verified["clean_post_id"], page_id, token, content)
                except antidetect_client.BrowserAutomationError as exc:
                    result = exc.as_result()
                except Exception:
                    result = {"success": False, "category": "NEED_CHECK", "error": "Chưa xác nhận được kết quả gửi. Đối chiếu trên Facebook."}
            if browser and result.get("category") in ("BROWSER_BUSY", "BROWSER_LIMIT", "BROWSER_BLOCKED"):
                attempts -= 1  # Profile contention must not consume another Page's send budget.
            _record_dispatch_result(task, result, decision.page)


def _record_dispatch_result(task, result, page):
    task_id, page_id = task["id"], task["page_id"]
    if result.get("usage_pct") is not None:
        db.update_page_api_usage(page_id, float(result["usage_pct"]))
    if result.get("success"):
        SCHEDULER_METRICS["send_errors"].pop(page_id, None)
        db.update_task_result(task_id, "SUCCESS", comment_id=result.get("comment_id"))
        return
    category = result.get("category", "UNKNOWN")
    error = result.get("error", "Lỗi không xác định")
    SCHEDULER_METRICS["send_errors"][page_id] = error
    if _send_mode(page) == "BROWSER":
        _record_browser_connection_failure(page, result)
        if category in ("BROWSER_BUSY", "BROWSER_LIMIT", "CANCELLED_BEFORE_SEND"):
            db.release_task_lock(task_id)
            retry_at = result.get("retry_at") or (
                datetime.now(timezone.utc) + timedelta(seconds=config.TASK_TICK_SEC)).isoformat()
            db.defer_task(task_id, retry_at, error)
            return
        if category == "BROWSER_BLOCKED":
            db.update_task_result(task_id, "PAUSED", error_message=error)
            return
        if result.get("browser_status") in ("LOGIN_REQUIRED", "CHECKPOINT", "RESTRICTED", "ERROR") and category not in (
                "NEED_CHECK", "NETWORK_RETRYABLE", "RATE_LIMIT", "POST_ERROR"):
            db.update_task_result(task_id, "PAUSED", error_message=error)
            return
    if category == "NEED_CHECK":
        db.update_task_result(task_id, "NEED_CHECK", error_message=error)
    elif category in ("TOKEN_EXPIRED", "MISSING_PERMISSIONS"):
        db.update_task_result(task_id, "PAUSED", error_message=error)
        status = "EXPIRED" if category == "TOKEN_EXPIRED" else category
        db.update_page_status(page_id, bool(page["is_active"]), status, error)
    elif category in ("RATE_LIMIT", "NETWORK_RETRYABLE"):
        if category == "RATE_LIMIT":
            retry_at = db.record_rate_limit(page_id, result)
            max_retries = 4
        else:
            retry_at = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
            max_retries = 3
        if task["retry_count"] < max_retries:
            with db.get_connection() as conn:
                conn.execute("""UPDATE tasks SET status='PENDING',locked_at=NULL,scheduled_at=?,
                    retry_count=retry_count+1,error_message=?,updated_at=? WHERE id=? AND status='SENDING'""",
                    (retry_at, error, datetime.now(timezone.utc).isoformat(), task_id))
        else:
            db.update_task_result(task_id, "FAILED", error_message=error)
    else:
        db.update_task_result(task_id, "FAILED", error_message=error)


def run_manual_sync_cycle():
    """Manual 'scan & run now' waits for in-flight loops instead of racing them."""
    if SCHEDULER_METRICS["paused_all"]:
        return
    with _SCAN_LOCK:
        _detect_new_posts_and_schedule()
    # Pause toggled while scanning: do not start posting the freshly scheduled tasks.
    if SCHEDULER_METRICS["paused_all"]:
        return
    with _EXEC_LOCK:
        _execute_due_tasks()


def resume_tasks(page_id=None, task_id=None, confirmed_not_posted=False):
    """Single preparation path for individual retries and Page-wide resumes."""
    with closing(db.get_connection()) as conn:
        if task_id is not None:
            rows = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tasks WHERE page_id=? AND status='PAUSED'", (page_id,)).fetchall()
    if not rows:
        return 0
    page_id = rows[0]["page_id"]
    allowed = {"FAILED", "PAUSED"} | ({"NEED_CHECK"} if confirmed_not_posted else set())
    if task_id is not None and rows[0]["status"] not in allowed:
        raise ValueError("Tác vụ Cần kiểm tra phải được đối chiếu và xác nhận chưa đăng trước khi gửi lại.")
    with comment_dispatch.page_lock(page_id):
        if not send_ready(db.get_page_for_dispatch(page_id) or {}):
            raise ValueError("Page chưa có kết nối khả dụng cho chế độ gửi đã chọn.")
        automatic = any(row["source"] == "AUTO" for row in rows)
        if any(row["source"] == "AUTO" and row["sequence_step"] is None for row in rows):
            result = refresh_comment_sheet(force=True)
            if not result.get("success"):
                raise ValueError(result.get("error") or "Chưa đọc được Sheet để khôi phục tác vụ.")
        with _COMMENT_SHEET_LOCK:
            page = db.get_page(page_id)
            if automatic and (not page["rule_enabled"] or page.get("source_error")):
                raise ValueError(page.get("source_error") or "Hãy bật tự động của Page trước khi khôi phục.")
            if task_id is not None:
                return int(db.retry_task(task_id, confirmed_not_posted=confirmed_not_posted))
            return db.resume_paused_tasks_for_page(page_id)


def run_maintenance_cycle():
    """
    Runs periodic maintenance:
    1. Recovers only tasks stuck in SENDING for > 5 minutes (startup=False).
    2. Checks token health ONLY when debug_token actually succeeded (`checked=True`).
    3. Refreshes Google Sheet configuration even while sending is paused.
    """
    db.recover_stale_tasks(startup=False, stale_after_seconds=300)

    for p in db.get_all_pages():
        pid = p["page_id"]
        if not p.get("rule_enabled") or _send_mode(p) == "BROWSER":
            continue
        full_p = db.get_page_for_dispatch(pid)
        if not full_p or not full_p.get("page_access_token"):
            continue
        info = meta_client.debug_token(full_p["page_access_token"])
        if info.get("checked") is not True:
            continue
        missing = info.get("missing_scopes") or []
        if not info.get("is_valid"):
            status, note = "EXPIRED", "Token không còn hợp lệ hoặc đã hết hạn."
        elif missing:
            status, note = "MISSING_PERMISSIONS", "Thiếu quyền: " + ", ".join(missing)
        else:
            # Token checks must never undo an operator pause or reconnect requirement.
            continue
        db.update_page_status(pid, bool(p["is_active"]), status, note)

    if comment_sheet_url():
        refresh_comment_sheet()


# --- 3 INDEPENDENT ASYNC LOOPS (NON-BLOCKING) ---

async def run_worker(worker):
    """Keep the service owner alive until a cancelled loop's actual thread exits."""
    pending = asyncio.create_task(asyncio.to_thread(worker))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        # Cancelling to_thread's awaiter does not stop an already running thread.
        await asyncio.gather(pending, return_exceptions=True)
        raise


async def loop_post_scanner():
    """Loop 1: Incremental post scanner (Interval: config.POST_SCAN_INTERVAL_SEC)."""
    while True:
        if not SCHEDULER_METRICS["paused_all"]:
            try:
                await run_worker(detect_new_posts_and_schedule)
                SCHEDULER_METRICS["post_scan"]["last_run"] = datetime.now(timezone.utc).isoformat()
                SCHEDULER_METRICS["post_scan"]["last_error"] = None
            except Exception as e:
                SCHEDULER_METRICS["post_scan"]["last_error"] = str(e)
        await asyncio.sleep(config.POST_SCAN_INTERVAL_SEC)


async def loop_task_executor():
    """Loop 2: Refresh Page templates while paused and dispatch enabled tasks."""
    while True:
        await run_worker(refresh_comment_sheet)
        if not SCHEDULER_METRICS["paused_all"]:
            try:
                await run_worker(execute_due_tasks)
                SCHEDULER_METRICS["task_exec"]["last_run"] = datetime.now(timezone.utc).isoformat()
                SCHEDULER_METRICS["task_exec"]["last_error"] = None
            except Exception as e:
                SCHEDULER_METRICS["task_exec"]["last_error"] = str(e)
        await asyncio.sleep(config.TASK_TICK_SEC)


async def loop_maintenance():
    """Loop 3: Token health debug, stale task recovery & auto sheet sync."""
    while True:
        try:
            await run_worker(run_maintenance_cycle)
            SCHEDULER_METRICS["maintenance"]["last_run"] = datetime.now(timezone.utc).isoformat()
            SCHEDULER_METRICS["maintenance"]["last_error"] = None
        except Exception as e:
            SCHEDULER_METRICS["maintenance"]["last_error"] = str(e)
        await asyncio.sleep(config.SHEET_SYNC_INTERVAL_SEC)
