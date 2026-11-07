import json
import random
import asyncio
from datetime import datetime, timezone, timedelta
from dateutil import parser as date_parser
from typing import Dict, Any, Optional

import config
import database as db
import meta_client
import spintax

# In-memory status for UI monitoring
SCHEDULER_METRICS = {
    "post_scan": {"last_run": None, "last_error": None, "status": "RUNNING"},
    "task_exec": {"last_run": None, "last_error": None, "status": "RUNNING"},
    "maintenance": {"last_run": None, "last_error": None, "status": "RUNNING"},
    "paused_all": False
}

def detect_new_posts_and_schedule():
    """
    Scans all active pages incrementally, detecting newly published posts.
    Calculates target delay and creates deduplicated auto-tasks.
    """
    pages = db.get_all_pages()
    now_utc = datetime.now(timezone.utc)

    for p in pages:
        if not p.get("is_active") or not p.get("rule_enabled"):
            continue

        page_id = p["page_id"]
        # Decrypt token only for worker execution
        page_full = db.get_page_with_decrypted_token(page_id)
        if not page_full:
            continue
        token = page_full.get("page_access_token", "")
        if not token:
            continue

        delay_minutes = p.get("delay_minutes", 10)
        since_ts = p.get("last_seen_post_time")

        # Parse templates
        try:
            templates = json.loads(p.get("templates", "[]"))
        except Exception:
            templates = []
        if not templates:
            continue

        try:
            posts = meta_client.get_recent_posts(page_id, token, since_timestamp=since_ts, limit=5)
        except meta_client.MetaAPIError as e:
            if e.category == "TOKEN_EXPIRED":
                db.update_page_status(page_id, is_active=False, token_status="EXPIRED", status_note=e.message)
            continue
        except Exception:
            continue

        latest_post_time = None
        for post in posts:
            post_id = post.get("id")
            created_str = post.get("created_time")
            if not post_id or not created_str:
                continue

            try:
                created_dt = date_parser.parse(created_str).astimezone(timezone.utc)
            except Exception:
                continue

            if latest_post_time is None or created_dt > latest_post_time:
                latest_post_time = created_dt

            # Skip posts that are older than MAX_POST_AGE_MIN
            age_minutes = (now_utc - created_dt).total_seconds() / 60.0
            if age_minutes > config.MAX_POST_AGE_MIN:
                continue

            # Calculate target schedule time
            target_run_time = created_dt + timedelta(minutes=delay_minutes)
            if target_run_time < now_utc:
                target_run_time = now_utc

            # Pick template and spin content
            selected_template = random.choice(templates)
            final_content = spintax.spin(selected_template)

            # Insert task (dedupe_key auto:{page_id}:{post_id} guarantees no duplicates)
            db.create_task(
                page_id=page_id,
                post_id=post_id,
                comment_content=final_content,
                scheduled_at=target_run_time.isoformat(),
                source="AUTO",
                post_created_time=created_dt.isoformat()
            )

        if latest_post_time:
            conn = db.get_connection()
            with conn:
                conn.execute("UPDATE pages SET last_seen_post_time = ? WHERE page_id = ?",
                             (latest_post_time.isoformat(), page_id))
            conn.close()

def execute_due_tasks():
    """
    Fetches tasks that are due, checks rate limits & interval gap, then posts comment.
    """
    due_tasks = db.get_due_tasks(limit=10)
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    for task in due_tasks:
        task_id = task["id"]
        page_id = task["page_id"]
        post_id = task["post_id"]
        token = task["page_access_token"]
        content = task["comment_content"]

        # Check Page daily limit
        max_per_day = task.get("max_comments_per_day") or config.MAX_COMMENTS_PER_PAGE_PER_DAY
        today_comments = task.get("comments_today_count") or 0
        if task.get("comments_today_date") == today_str and today_comments >= max_per_day:
            # Postpone task to tomorrow
            tomorrow_utc = (now_utc + timedelta(days=1)).replace(hour=1, minute=0, second=0).isoformat()
            conn = db.get_connection()
            with conn:
                conn.execute("UPDATE tasks SET scheduled_at = ? WHERE id = ?", (tomorrow_utc, task_id))
            conn.close()
            continue

        # Check Minimum Gap between comments on same Page
        last_comment_str = task.get("last_comment_at")
        if last_comment_str:
            try:
                last_dt = date_parser.parse(last_comment_str).astimezone(timezone.utc)
                if (now_utc - last_dt).total_seconds() < config.MIN_GAP_PER_PAGE_SEC:
                    # Delay this task slightly to preserve gap
                    continue
            except Exception:
                pass

        # Check if task is overdue by > 2 hours (e.g. system was off)
        sched_dt = date_parser.parse(task["scheduled_at"]).astimezone(timezone.utc)
        if (now_utc - sched_dt).total_seconds() > 7200:
            db.update_task_result(
                task_id=task_id,
                status="NEED_CHECK",
                error_message="Tác vụ quá hạn hơn 2 giờ do hệ thống tạm dừng. Vui lòng đối chiếu bài viết trước khi bấm gửi lại."
            )
            continue

        # Lock task for sending
        if not db.lock_task_for_sending(task_id):
            continue

        # Post comment to Meta
        result = meta_client.post_comment(
            post_id=post_id,
            page_id=page_id,
            page_access_token=token,
            message=content
        )

        if result.get("success"):
            db.update_task_result(
                task_id=task_id,
                status="SUCCESS",
                comment_id=result.get("comment_id")
            )
        else:
            category = result.get("category", "UNKNOWN")
            err_msg = result.get("error", "Lỗi không xác định")

            if category == "NEED_CHECK":
                db.update_task_result(task_id=task_id, status="NEED_CHECK", error_message=err_msg)
            elif category == "TOKEN_EXPIRED":
                db.update_task_result(task_id=task_id, status="PAUSED", error_message=err_msg)
                db.update_page_status(page_id, is_active=False, token_status="EXPIRED", status_note=err_msg)
            elif category == "RATE_LIMIT":
                retry_count = task.get("retry_count", 0)
                if retry_count < 4:
                    backoff_mins = 2 ** (retry_count + 1)
                    new_time = (now_utc + timedelta(minutes=backoff_mins)).isoformat()
                    conn = db.get_connection()
                    with conn:
                        conn.execute("UPDATE tasks SET status = 'PENDING', scheduled_at = ?, retry_count = retry_count + 1 WHERE id = ?",
                                     (new_time, task_id))
                    conn.close()
                else:
                    db.update_task_result(task_id=task_id, status="FAILED", error_message=err_msg)
            elif category == "NETWORK_RETRYABLE":
                retry_count = task.get("retry_count", 0)
                if retry_count < 3:
                    new_time = (now_utc + timedelta(minutes=2)).isoformat()
                    conn = db.get_connection()
                    with conn:
                        conn.execute("UPDATE tasks SET status = 'PENDING', scheduled_at = ?, retry_count = retry_count + 1 WHERE id = ?",
                                     (new_time, task_id))
                    conn.close()
                else:
                    db.update_task_result(task_id=task_id, status="FAILED", error_message=err_msg)
            else:
                db.update_task_result(task_id=task_id, status="FAILED", error_message=err_msg)

# --- 3 INDEPENDENT ASYNC LOOPS (NON-BLOCKING) ---

async def loop_post_scanner():
    """Loop 1: Incremental post scanner (Interval: config.POST_SCAN_INTERVAL_SEC)."""
    while True:
        if not SCHEDULER_METRICS["paused_all"]:
            try:
                await asyncio.to_thread(detect_new_posts_and_schedule)
                SCHEDULER_METRICS["post_scan"]["last_run"] = datetime.now(timezone.utc).isoformat()
                SCHEDULER_METRICS["post_scan"]["last_error"] = None
            except Exception as e:
                SCHEDULER_METRICS["post_scan"]["last_error"] = str(e)
        await asyncio.sleep(config.POST_SCAN_INTERVAL_SEC)

async def loop_task_executor():
    """Loop 2: Task execution tick (Interval: config.TASK_TICK_SEC)."""
    while True:
        if not SCHEDULER_METRICS["paused_all"]:
            try:
                await asyncio.to_thread(execute_due_tasks)
                SCHEDULER_METRICS["task_exec"]["last_run"] = datetime.now(timezone.utc).isoformat()
                SCHEDULER_METRICS["task_exec"]["last_error"] = None
            except Exception as e:
                SCHEDULER_METRICS["task_exec"]["last_error"] = str(e)
        await asyncio.sleep(config.TASK_TICK_SEC)

async def loop_maintenance():
    """Loop 3: Token health debug & recover stale tasks (Interval: 1 hour)."""
    while True:
        try:
            await asyncio.to_thread(db.recover_stale_tasks)
            # Inspect token health for active pages
            pages = await asyncio.to_thread(db.get_all_pages)
            for p in pages:
                pid = p["page_id"]
                full_p = await asyncio.to_thread(db.get_page_with_decrypted_token, pid)
                if full_p and full_p.get("page_access_token"):
                    info = await asyncio.to_thread(meta_client.debug_token, full_p["page_access_token"])
                    if not info.get("is_valid"):
                        await asyncio.to_thread(db.update_page_status, pid, False, "EXPIRED", "Token không còn hợp lệ")
                    elif info.get("missing_scopes"):
                        await asyncio.to_thread(db.update_page_status, pid, False, "MISSING_PERMISSIONS", f"Thiếu quyền: {info.get('missing_scopes')}")

            SCHEDULER_METRICS["maintenance"]["last_run"] = datetime.now(timezone.utc).isoformat()
            SCHEDULER_METRICS["maintenance"]["last_error"] = None
        except Exception as e:
            SCHEDULER_METRICS["maintenance"]["last_error"] = str(e)
        await asyncio.sleep(3600)
