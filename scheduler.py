import json
import random
import asyncio
from datetime import datetime, timezone, timedelta
from dateutil import parser as date_parser
from typing import Dict, Any, Optional

import config
import database as db
import meta_client
import antidetect_client
import sheets_sync
import spintax


# In-memory status for UI monitoring
SCHEDULER_METRICS: Dict[str, Any] = {
    "post_scan": {"last_run": None, "last_error": None, "status": "RUNNING"},
    "task_exec": {"last_run": None, "last_error": None, "status": "RUNNING"},
    "maintenance": {"last_run": None, "last_error": None, "status": "RUNNING"},
    "sheet_sync": {"last_run": None, "last_error": None, "sheet_url": config.DEFAULT_SHEET_URL},
    "paused_all": config.PAUSED_BY_DEFAULT


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

        # Skip scanning if page API usage is critically high (>= 90%)
        if (p.get("api_usage_percent") or 0.0) >= 90.0:
            continue

        page_id = p["page_id"]
        page_full = db.get_page_with_decrypted_token(page_id)
        if not page_full:
            continue
        token = page_full.get("page_access_token", "")
        profile_id = p.get("antidetect_profile_id") or page_full.get("antidetect_profile_id")
        if not token and not profile_id:
            continue

        delay_minutes = p.get("delay_minutes", 10)
        since_ts = p.get("last_seen_post_time")

        try:
            templates = json.loads(p.get("templates", "[]"))
        except Exception:
            templates = []
        if not templates:
            continue

        posts = []
        if token:
            try:
                posts = meta_client.get_recent_posts(page_id, token, since_timestamp=since_ts, limit=5)
            except meta_client.MetaAPIError as e:
                if e.category == "TOKEN_EXPIRED":
                    if profile_id:
                        # Hybrid fallback: keep active on Antidetect if Graph token expired
                        db.update_page_status(page_id, is_active=True, token_status="ANTIDETECT_READY", status_note="Token API hết hạn, đã chuyển sang chế độ Antidetect CDP")
                    else:
                        db.update_page_status(page_id, is_active=False, token_status="EXPIRED", status_note=e.message)
                continue
            except Exception:
                continue
        elif profile_id:
            # For Antidetect-only pages, scan automatically if the profile is currently open
            # or if ANTIDETECT_AUTO_SCAN_POSTS is enabled
            rt = antidetect_client._read_runtime_readonly().get(profile_id) or {}
            is_open = antidetect_client._probe_cdp_port(rt.get("port"))
            if not is_open and not config.ANTIDETECT_AUTO_SCAN_POSTS:
                continue
            try:
                posts = antidetect_client.get_recent_posts_cdp(
                    page_id=page_id,
                    page_url=p.get("page_url"),
                    profile_id=profile_id,
                    since_timestamp=since_ts,
                    limit=5,
                )
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
            post_url = post.get("permalink_url")
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
                post_created_time=created_dt.isoformat(),
                post_url=post_url
            )


        if latest_post_time:
            conn = db.get_connection()
            with conn:
                conn.execute(
                    "UPDATE pages SET last_seen_post_time = ? WHERE page_id = ?",
                    (latest_post_time.isoformat(), page_id)
                )
            conn.close()


def execute_due_tasks():
    """
    Fetches tasks that are due, re-verifies live per-page rate limits & minimum gap
    before each task so multiple due tasks for the same page never violate constraints,
    then posts comments to Meta.
    """
    due_tasks = db.get_due_tasks(limit=10)
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    for task in due_tasks:
        task_id = task["id"]
        page_id = task["page_id"]
        post_id = task["post_id"]
        token = task["page_access_token"]
        content = (task["comment_content"] or "").strip()

        # Re-fetch live page state from DB so earlier tasks in the same batch are accounted for!
        fresh_page = db.get_page(page_id)
        if not fresh_page or not fresh_page.get("is_active"):
            continue

        # 1. Check Page daily limit using fresh DB count
        max_per_day = fresh_page.get("max_comments_per_day") or config.MAX_COMMENTS_PER_PAGE_PER_DAY
        today_comments = fresh_page.get("comments_today_count") or 0
        if fresh_page.get("comments_today_date") == today_str and today_comments >= max_per_day:
            tomorrow_utc = (now_utc + timedelta(days=1)).replace(hour=1, minute=0, second=0).isoformat()
            conn = db.get_connection()
            with conn:
                conn.execute("UPDATE tasks SET scheduled_at = ? WHERE id = ?", (tomorrow_utc, task_id))
            conn.close()
            continue

        # 2. Check Minimum Gap between comments on the same Page using fresh last_comment_at
        last_comment_str = fresh_page.get("last_comment_at")
        if last_comment_str:
            try:
                last_dt = date_parser.parse(last_comment_str).astimezone(timezone.utc)
                elapsed_sec = (now_utc - last_dt).total_seconds()
                if elapsed_sec < config.MIN_GAP_PER_PAGE_SEC:
                    next_slot = (last_dt + timedelta(seconds=config.MIN_GAP_PER_PAGE_SEC)).isoformat()
                    conn = db.get_connection()
                    with conn:
                        conn.execute("UPDATE tasks SET scheduled_at = ? WHERE id = ? AND status = 'PENDING'", (next_slot, task_id))
                    conn.close()
                    continue
            except Exception:
                pass

        # 3. Check if task is overdue by > 2 hours (e.g. system was off overnight)
        try:
            sched_dt = date_parser.parse(task["scheduled_at"]).astimezone(timezone.utc)
            if (now_utc - sched_dt).total_seconds() > 7200:
                db.update_task_result(
                    task_id=task_id,
                    status="NEED_CHECK",
                    error_message="Tác vụ quá hạn hơn 2 giờ do hệ thống tạm dừng. Vui lòng đối chiếu bài viết trước khi bấm gửi lại."
                )
                continue
        except Exception:
            pass

        # 4. Final Spintax / brace safety check before sending to Facebook
        if "{" in content or "}" in content:
            if not sheets_sync.validate_spintax_braces(content):
                db.update_task_result(
                    task_id=task_id,
                    status="FAILED",
                    error_message="Nội dung bình luận chứa cú pháp ngoặc Spintax '{...}' bị lỗi nên đã chặn gửi."
                )
                continue
            content = spintax.spin(content)

        # 5. Lock task for sending
        if not db.lock_task_for_sending(task_id):
            continue

        # 6. Post comment to Meta (via Graph API if token available, else via Antidetect CDP)
        profile_id = fresh_page.get("antidetect_profile_id") or task.get("antidetect_profile_id")
        if token and fresh_page.get("token_status") not in ("EXPIRED", "MISSING_PERMISSIONS", "ANTIDETECT_READY"):
            result = meta_client.post_comment(
                post_id=post_id,
                page_id=page_id,
                page_access_token=token,
                message=content
            )
            # Hybrid fallback: if Graph API token expired/failed auth and page has Antidetect profile
            if not result.get("success") and result.get("category") == "TOKEN_EXPIRED" and profile_id:
                result = antidetect_client.post_comment_cdp(
                    post_id=post_id,
                    page_id=fresh_page.get("fb_profile_id") or page_id,
                    page_name=fresh_page.get("page_name") or page_id,
                    page_url=fresh_page.get("page_url"),
                    profile_id=profile_id,
                    message=content,
                    post_url=task.get("post_url"),
                )
        elif profile_id:
            result = antidetect_client.post_comment_cdp(
                post_id=post_id,
                page_id=fresh_page.get("fb_profile_id") or page_id,
                page_name=fresh_page.get("page_name") or page_id,
                page_url=fresh_page.get("page_url"),
                profile_id=profile_id,
                message=content,
                post_url=task.get("post_url"),
            )
        else:
            result = {
                "success": False,
                "category": "TOKEN_EXPIRED",
                "error": "Page chưa có Token Graph API và cũng chưa gắn Profile Antidetect.",
                "usage_pct": None,
            }


        if result.get("usage_pct") is not None:
            db.update_page_api_usage(page_id, float(result["usage_pct"]))

        if result.get("success"):
            db.update_task_result(
                task_id=task_id,
                status="SUCCESS",
                comment_id=result.get("comment_id")
            )
            now_utc = datetime.now(timezone.utc)
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
                        conn.execute(
                            "UPDATE tasks SET status = 'PENDING', locked_at = NULL, scheduled_at = ?, retry_count = retry_count + 1, error_message = ? WHERE id = ?",
                            (new_time, err_msg, task_id)
                        )
                    conn.close()
                else:
                    db.update_task_result(task_id=task_id, status="FAILED", error_message=err_msg)
            elif category == "NETWORK_RETRYABLE":
                retry_count = task.get("retry_count", 0)
                if retry_count < 3:
                    new_time = (now_utc + timedelta(minutes=2)).isoformat()
                    conn = db.get_connection()
                    with conn:
                        conn.execute(
                            "UPDATE tasks SET status = 'PENDING', locked_at = NULL, scheduled_at = ?, retry_count = retry_count + 1, error_message = ? WHERE id = ?",
                            (new_time, err_msg, task_id)
                        )
                    conn.close()
                else:
                    db.update_task_result(task_id=task_id, status="FAILED", error_message=err_msg)
            else:
                db.update_task_result(task_id=task_id, status="FAILED", error_message=err_msg)


def run_maintenance_cycle():
    """
    Runs periodic maintenance:
    1. Recovers only tasks stuck in SENDING for > 5 minutes (startup=False).
    2. Checks token health ONLY when debug_token actually succeeded (`checked=True`).
    3. Auto-syncs Google Sheet if a Sheet URL is saved.
    """
    db.recover_stale_tasks(startup=False, stale_after_seconds=300)

    pages = db.get_all_pages()
    for p in pages:
        pid = p["page_id"]
        full_p = db.get_page_with_decrypted_token(pid)
        if full_p and full_p.get("page_access_token"):
            info = meta_client.debug_token(full_p["page_access_token"])
            # CRITICAL: Only act if Meta explicitly returned a token inspection result (`checked=True`)
            if info.get("checked") is True:
                if not info.get("is_valid"):
                    db.update_page_status(pid, False, "EXPIRED", "Token không còn hợp lệ hoặc đã hết hạn")
                elif info.get("missing_scopes"):
                    db.update_page_status(
                        pid,
                        False,
                        "MISSING_PERMISSIONS",
                        f"Thiếu quyền: {', '.join(info['missing_scopes'])}"
                    )
                else:
                    db.update_page_status(pid, True, info.get("status", "OK"), None)

    sheet_url = SCHEDULER_METRICS["sheet_sync"].get("sheet_url")
    if sheet_url:
        res = sheets_sync.sync_from_google_sheet(sheet_url)
        SCHEDULER_METRICS["sheet_sync"]["last_run"] = datetime.now(timezone.utc).isoformat()
        SCHEDULER_METRICS["sheet_sync"]["last_error"] = None if res.get("success") else res.get("error")


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
    """Loop 3: Token health debug, stale task recovery & auto sheet sync."""
    while True:
        try:
            await asyncio.to_thread(run_maintenance_cycle)
            SCHEDULER_METRICS["maintenance"]["last_run"] = datetime.now(timezone.utc).isoformat()
            SCHEDULER_METRICS["maintenance"]["last_error"] = None
        except Exception as e:
            SCHEDULER_METRICS["maintenance"]["last_error"] = str(e)
        await asyncio.sleep(config.SHEET_SYNC_INTERVAL_SEC)
