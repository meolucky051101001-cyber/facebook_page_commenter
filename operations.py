import asyncio
import json
import re
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
import httpx

import config
import comment_dispatch
import database as db
import meta_client
import scheduler
from sheets_sync import validate_spintax_braces
from spintax import spin


_publication_lock = threading.Lock()
_inbox_lock = threading.Lock()
_phone = re.compile(r"(?<!\d)(?:0|\+?84)(?:[ .-]?\d){8,10}(?!\d)")
_id = re.compile(r"[0-9_]+")


def add_publications(entries):
    rows = []
    for entry in entries:
        page = db.get_page(entry.page_id.strip())
        if not page:
            raise ValueError("Page không tồn tại hoặc ID trùng.")
        if entry.scheduled_at.utcoffset() is None:
            raise ValueError("Giờ xuất bản cần kèm múi giờ, ví dụ +07:00.")
        if not entry.external_key.strip():
            raise ValueError("Mã lịch không được để trống.")
        media = urlsplit(entry.media_url.strip())
        if entry.media_type != "text" and (
            media.scheme != "https" or not media.hostname or media.username or media.password
        ):
            raise ValueError("Ảnh và Reel cần URL HTTPS mà Meta có thể đọc.")
        if entry.media_type == "text" and not entry.caption.strip():
            raise ValueError("Bài văn bản cần nội dung.")
        if not all(validate_spintax_braces(text) for text in (entry.caption, entry.first_comment)):
            raise ValueError("Kiểm tra dấu ngoặc trong nội dung bài và bình luận.")
        rows.append((
            page["page_id"], entry.external_key, entry.media_type, entry.media_url.strip(),
            entry.caption, entry.first_comment, entry.comment_delay_minutes,
            entry.scheduled_at.astimezone(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
        ))
    with closing(db.get_connection()) as conn, conn:
        conn.executemany("""
            INSERT INTO publications (page_id, external_key, media_type, media_url, caption,
                first_comment, comment_delay_minutes, scheduled_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(external_key) DO NOTHING
        """, rows)
        return conn.total_changes


def list_publications():
    with closing(db.get_connection()) as conn:
        return [dict(row) for row in conn.execute("""
            SELECT p.id, p.page_id, a.page_name, p.media_type, p.caption, p.scheduled_at,
                   p.status, p.post_id, p.error
            FROM publications p JOIN pages a ON a.page_id = p.page_id
            ORDER BY p.id DESC LIMIT 100
        """)]


def cancel_publication(publication_id):
    with closing(db.get_connection()) as conn, conn:
        return bool(conn.execute(
            "UPDATE publications SET status = 'CANCELLED' WHERE id = ? AND status = 'PENDING'",
            (publication_id,),
        ).rowcount)


def recover_interrupted_operations(conn):
    with conn:
        conn.execute("""
            UPDATE publications SET status = 'NEED_CHECK',
                error = 'Chưa rõ Facebook đã nhận lệnh. Đối chiếu Page trước khi lên lịch lại.'
            WHERE status = 'PUBLISHING'
        """)
        conn.execute("""
            UPDATE incoming_comments SET action_pending = 'NEED_CHECK'
            WHERE action_pending IS NOT NULL
        """)


def process_publications():
    if scheduler.SCHEDULER_METRICS["paused_all"] or not _publication_lock.acquire(blocking=False):
        return
    try:
        with scheduler._SCAN_LOCK:
            with closing(db.get_connection()) as conn:
                jobs = conn.execute("""
                    SELECT * FROM publications
                    WHERE status = 'PROCESSING' OR (status = 'PENDING' AND scheduled_at <= ?)
                    ORDER BY scheduled_at LIMIT 10
                """, (datetime.now(timezone.utc).isoformat(),)).fetchall()
            for job in jobs:
                if scheduler.SCHEDULER_METRICS["paused_all"]:
                    break
                _process_publication(job)
    finally:
        _publication_lock.release()


def _publication_state(job, status, error=None, post_id=None):
    with closing(db.get_connection()) as conn, conn:
        conn.execute("""
            UPDATE publications SET status = ?, error = ?, post_id = COALESCE(?, post_id), updated_at = ?
            WHERE id = ?
        """, (status, error, post_id, datetime.now(timezone.utc).isoformat(), job["id"]))


def _process_publication(job):
    page = db.get_page_with_decrypted_token(job["page_id"])
    if not page or not scheduler.graph_ready(page):
        if job["status"] == "PENDING":
            with closing(db.get_connection()) as conn, conn:
                conn.execute("""
                    UPDATE publications SET status = 'FAILED', error = 'Page cần kết nối Meta có quyền xuất bản.'
                    WHERE id = ? AND status = 'PENDING'
                """, (job["id"],))
        elif datetime.now(timezone.utc) - datetime.fromisoformat(job["updated_at"]) > timedelta(hours=1):
            _publication_state(job, "NEED_CHECK", "Kết nối Page không còn khả dụng để kiểm tra bài.")
        return
    page_id, token = page["page_id"], page["page_access_token"]
    post_id = job["post_id"]
    started_at = datetime.fromisoformat(job["updated_at"])
    if job["status"] == "PENDING":
        now = datetime.now(timezone.utc)
        if now - datetime.fromisoformat(job["scheduled_at"]) > timedelta(hours=2):
            with closing(db.get_connection()) as conn, conn:
                conn.execute("""
                    UPDATE publications SET status = 'FAILED', error = 'Lịch đăng đã quá hạn 2 giờ.'
                    WHERE id = ? AND status = 'PENDING'
                """, (job["id"],))
            return
        with closing(db.get_connection()) as conn, conn:
            claimed = conn.execute("""
                UPDATE publications SET status = 'PUBLISHING', updated_at = ?
                WHERE id = ? AND status = 'PENDING'
            """, (now.isoformat(), job["id"])).rowcount
        if not claimed:
            return
        if scheduler.SCHEDULER_METRICS["paused_all"]:
            _publication_state(job, "PENDING")
            return
        try:
            post_id = meta_client.publish_page_post(
                page_id, token, job["media_type"], job["media_url"], spin(job["caption"]),
            )
            _publication_state(job, "PROCESSING", post_id=post_id)
            started_at = now
        except meta_client.MetaAPIError as exc:
            _publication_state(job, "NEED_CHECK", exc.message)
            return
        except Exception:
            _publication_state(job, "NEED_CHECK", "Lỗi xuất bản, cần đối chiếu bài trên Facebook.")
            return

    try:
        if job["media_type"] == "reel":
            video = meta_client.page_request("GET", post_id, token, {"fields": "status"})
            phase = video.get("status", {}).get("publishing_phase", {}).get("status")
            if phase != "complete":
                raise ValueError("Reel chưa xuất bản xong.")
        result = meta_client.verify_post_belongs_to_page(post_id, page_id, token)
        created_at = datetime.fromisoformat(result["created_time"])
        if created_at.utcoffset() is None:
            raise ValueError("Thiếu múi giờ xuất bản.")
        published_at = (datetime.now(timezone.utc) if job["media_type"] == "reel"
                        else created_at.astimezone(timezone.utc))
    except Exception:
        if datetime.now(timezone.utc) - started_at > timedelta(hours=1):
            _publication_state(job, "NEED_CHECK", "Chưa xác minh được bài xuất bản. Kiểm tra Page.", post_id)
        return

    canonical_post = result["clean_post_id"]
    sheet_ready = (
        not job["first_comment"].strip() and page.get("rule_enabled")
        and scheduler.refresh_comment_sheet()["success"]
    )
    with closing(db.get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        already_queued = conn.execute("""
            SELECT 1 FROM tasks WHERE page_id = ? AND post_id = ? AND source = 'AUTO'
        """, (page_id, canonical_post)).fetchone()
        if job["first_comment"].strip() and not already_queued:
            due = published_at + timedelta(minutes=job["comment_delay_minutes"])
            conn.execute("""
                INSERT INTO tasks (page_id, post_id, post_url, dedupe_key, source,
                    comment_content, scheduled_at, post_created_time)
                VALUES (?, ?, ?, ?, 'MANUAL', ?, ?, ?)
                ON CONFLICT(dedupe_key) DO NOTHING
            """, (
                page_id, canonical_post, result.get("permalink_url"), f"publication:{job['id']}",
                spin(job["first_comment"].replace("{page_name}", page["page_name"])),
                due.isoformat(), published_at.isoformat(),
            ))
        elif sheet_ready:
            current_rule = conn.execute("""
                SELECT p.page_id, p.page_name, r.delay_minutes, r.templates, r.comment_sequence
                FROM page_rules r JOIN pages p ON p.page_id = r.page_id
                WHERE r.page_id = ? AND p.is_active = 1 AND r.is_enabled = 1 AND r.source_error IS NULL
            """, (page_id,)).fetchone()
            if current_rule and current_rule["templates"] != "[]":
                db.insert_auto_post_tasks(conn, page_id, scheduler.build_post_schedule(
                    dict(current_rule), canonical_post, result.get("permalink_url"), published_at,
                    datetime.now(timezone.utc),
                ))
        conn.execute("""
            UPDATE publications SET status = 'SUCCESS', post_id = ?, error = NULL, updated_at = ?
            WHERE id = ? AND status = 'PROCESSING'
        """, (canonical_post, datetime.now(timezone.utc).isoformat(), job["id"]))


def refresh_inbox(page_id, since=None):
    page = db.get_page_with_decrypted_token(page_id)
    if not page or not scheduler.graph_ready(page):
        raise ValueError("Page cần kết nối Meta hợp lệ.")
    page_id, token = page["page_id"], page["page_access_token"]
    seen = set()
    with _inbox_lock:
        since = since or (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        for post in meta_client.get_recent_posts(page_id, token, since_timestamp=since):
            post_id = str(post["id"])
            if not _id.fullmatch(post_id):
                continue
            params = {"fields": "id,from,message,created_time,is_hidden", "limit": 50}
            cursors = set()
            while True:
                payload = meta_client.page_request("GET", f"{post_id}/comments", token, params)
                with closing(db.get_connection()) as conn, conn:
                    for comment in payload.get("data", []):
                        sender = comment.get("from") or {}
                        comment_id = str(comment.get("id", ""))
                        if str(sender.get("id")) == page_id or not _id.fullmatch(comment_id):
                            continue
                        message = comment.get("message") or ""
                        conn.execute("""
                            INSERT INTO incoming_comments
                                (comment_id, page_id, post_id, author_name, message, has_phone, created_at, hidden)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(comment_id) DO UPDATE SET message = excluded.message,
                                has_phone = excluded.has_phone, hidden = excluded.hidden
                        """, (
                            comment_id, page_id, post_id, sender.get("name", ""), _phone.sub("[số điện thoại]", message),
                            bool(_phone.search(message)),
                            comment.get("created_time", ""), bool(comment.get("is_hidden")),
                        ))
                        seen.add(comment_id)
                paging = payload.get("paging", {})
                if not paging.get("next"):
                    break
                cursor = paging.get("cursors", {}).get("after")
                if not cursor or cursor in cursors or len(cursors) >= 20:
                    raise ValueError("Đã lưu các bình luận đọc được. Meta chưa trả hết danh sách.")
                cursors.add(cursor)
                params["after"] = cursor
    return len(seen)


def list_inbox(page_id=None):
    with closing(db.get_connection()) as conn:
        rows = conn.execute("""
            SELECT c.*, p.page_name FROM incoming_comments c JOIN pages p ON p.page_id = c.page_id
            WHERE (? IS NULL OR c.page_id = ?) ORDER BY c.created_at DESC LIMIT 200
        """, (page_id, page_id)).fetchall()
    return [dict(row) for row in rows]


def set_moderation_rules(page_id, hide_phone, keyword, reply):
    page = db.get_page(page_id)
    if not page:
        raise ValueError("Page không tồn tại hoặc ID trùng.")
    keyword, reply = keyword.strip(), reply.strip()
    if bool(keyword) != bool(reply):
        raise ValueError("Nhập cả từ khóa và nội dung trả lời, hoặc để trống cả hai.")
    rules = {"hide_phone": hide_phone, "keyword": keyword, "reply": reply,
             "since": datetime.now(timezone.utc).isoformat()}
    db.set_setting("moderation:" + page["page_id"], json.dumps(rules, ensure_ascii=False))
    return rules


def get_moderation_rules(page_id):
    return json.loads(db.get_setting("moderation:" + page_id, "{}"))


def apply_moderation_rules(page_id):
    rules = get_moderation_rules(page_id)
    if not rules.get("hide_phone") and not rules.get("keyword"):
        return
    keyword = rules["keyword"]
    pattern = re.compile(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", re.IGNORECASE) if keyword else None
    with closing(db.get_connection()) as conn:
        conn.create_function("matches_reply_keyword", 1, lambda message: int(bool(pattern and pattern.search(message))))
        rows = conn.execute("""
            SELECT * FROM incoming_comments WHERE page_id = ? AND action_pending IS NULL
              AND julianday(created_at) >= julianday(?)
              AND ((? AND has_phone = 1 AND hidden = 0) OR
                   (reply_id IS NULL AND matches_reply_keyword(message)))
            ORDER BY created_at LIMIT 200
        """, (page_id, rules["since"], rules["hide_phone"])).fetchall()
    for item in rows:
        if scheduler.SCHEDULER_METRICS["paused_all"] or get_moderation_rules(page_id) != rules:
            break
        if rules["hide_phone"] and item["has_phone"] and not item["hidden"]:
            moderate(item["comment_id"], "hide")
        if (pattern and not item["reply_id"] and pattern.search(item["message"])
                and not scheduler.SCHEDULER_METRICS["paused_all"]):
            result = moderate(item["comment_id"], "reply", rules["reply"], automatic=True, expected_rules=rules)
            if result.get("waiting"):
                break


def refresh_enabled_inboxes():
    if scheduler.SCHEDULER_METRICS["paused_all"] or db.get_setting("inbox_monitor") != "enabled":
        return
    errors = scheduler.SCHEDULER_METRICS.setdefault("inbox_errors", {})
    for page in db.get_all_pages():
        if scheduler.SCHEDULER_METRICS["paused_all"]:
            break
        if not page["rule_enabled"] or not page["has_page_token"]:
            continue
        try:
            refresh_inbox(page["page_id"])
            apply_moderation_rules(page["page_id"])
            errors.pop(page["page_id"], None)
        except (ValueError, meta_client.MetaAPIError) as exc:
            errors[page["page_id"]] = str(exc)
        except Exception:
            errors[page["page_id"]] = "Không đọc được bình luận từ Meta."


def moderate(comment_id, action, message="", *, automatic=False, expected_rules=None):
    if not _id.fullmatch(comment_id):
        raise ValueError("ID bình luận không hợp lệ.")
    if action == "reply":
        return _reply(comment_id, message, automatic=automatic, expected_rules=expected_rules)
    if action not in ("hide", "unhide"):
        raise ValueError("Thao tác bình luận không hợp lệ.")
    with _inbox_lock, closing(db.get_connection()) as conn:
        item = conn.execute("SELECT * FROM incoming_comments WHERE comment_id=?", (comment_id,)).fetchone()
        if not item:
            raise ValueError("Bình luận chưa có trong hộp thư.")
        page = db.get_page_for_dispatch(item["page_id"])
        if not comment_dispatch.graph_ready(page):
            raise ValueError("Page cần kết nối Meta hợp lệ.")
        with conn:
            claimed = conn.execute("UPDATE incoming_comments SET action_pending=? WHERE comment_id=? AND action_pending IS NULL",
                                   (action, comment_id)).rowcount
        if not claimed:
            raise ValueError("Bình luận đang xử lý hoặc cần đối chiếu trên Facebook.")
        try:
            hidden = action == "hide"
            result = meta_client.page_request("POST", comment_id, page["page_access_token"], {"is_hidden": str(hidden).lower()})
            if result.get("success") is not True:
                raise ValueError("Meta chưa xác nhận cập nhật bình luận.")
            with conn:
                conn.execute("UPDATE incoming_comments SET hidden=?,action_pending=NULL WHERE comment_id=?", (hidden, comment_id))
        except Exception as exc:
            _store_inbox_failure(conn, comment_id, exc)
            raise ValueError("Chưa xác nhận được kết quả. Đối chiếu trên Facebook.") from None
    return {"success": True}


def _store_inbox_failure(conn, comment_id, exc):
    pending = None if isinstance(exc, meta_client.MetaAPIError) and exc.category in {
        "MISSING_PERMISSIONS", "TOKEN_EXPIRED", "RATE_LIMIT", "POST_ERROR"} else "NEED_CHECK"
    with conn:
        conn.execute("UPDATE incoming_comments SET action_pending=? WHERE comment_id=?", (pending, comment_id))


def _reply(comment_id, message, *, automatic=False, expected_rules=None):
    if not message.strip() or len(message) > 8000:
        raise ValueError("Nhập nội dung trả lời từ 1 đến 8000 ký tự.")
    with closing(db.get_connection()) as conn:
        item = conn.execute("SELECT * FROM incoming_comments WHERE comment_id=?", (comment_id,)).fetchone()
        if not item:
            raise ValueError("Bình luận chưa có trong hộp thư.")
        page_id = item["page_id"]
        paused = lambda: scheduler.SCHEDULER_METRICS["paused_all"]
        with comment_dispatch.guard(page_id, paused, automatic=automatic) as decision:
            if item["reply_id"] or item["action_pending"]:
                raise ValueError("Bình luận đã được trả lời hoặc cần đối chiếu trên Facebook.")
            if not decision.ready:
                return decision.public()
            if automatic and expected_rules != get_moderation_rules(page_id):
                return comment_dispatch.Decision("AUTOMATION_PAUSED", message="Quy tắc trả lời đã thay đổi.").public()
            with conn:
                claimed = conn.execute("""UPDATE incoming_comments SET action_pending='reply'
                    WHERE comment_id=? AND reply_id IS NULL AND action_pending IS NULL""", (comment_id,)).rowcount
            if not claimed:
                raise ValueError("Bình luận đang xử lý hoặc đã được trả lời.")
            latest = comment_dispatch.check(page_id, paused, automatic=automatic)
            rules_changed = automatic and expected_rules != get_moderation_rules(page_id)
            if not latest.ready or latest.page["page_access_token"] != decision.page["page_access_token"] or rules_changed:
                with conn:
                    conn.execute("UPDATE incoming_comments SET action_pending=NULL WHERE comment_id=? AND action_pending='reply'", (comment_id,))
                return latest.public() if not latest.ready else comment_dispatch.Decision("PAGE_CHANGED", message="Kết nối hoặc quy tắc Page đã đổi. Vui lòng thử lại.").public()
            try:
                reply = meta_client.page_request("POST", f"{comment_id}/comments", latest.page["page_access_token"], {"message": message}).get("id")
                if not reply:
                    raise ValueError("Meta chưa trả ID bình luận.")
                with conn:
                    changed = conn.execute("""UPDATE incoming_comments SET reply_id=?,action_pending=NULL
                        WHERE comment_id=? AND reply_id IS NULL AND action_pending='reply'""", (reply, comment_id)).rowcount
                    if changed:
                        db.record_comment_success(conn, page_id, datetime.now(timezone.utc))
            except meta_client.MetaAPIError as exc:
                _store_inbox_failure(conn, comment_id, exc)
                if exc.category == "RATE_LIMIT":
                    until = db.record_rate_limit(page_id, exc.as_result())
                    return comment_dispatch.Decision("WAIT_RATE_LIMIT", retry_at=until, message=exc.message).public()
                if exc.category in ("TOKEN_EXPIRED", "MISSING_PERMISSIONS"):
                    db.update_page_status(page_id, bool(latest.page["is_active"]),
                                          "EXPIRED" if exc.category == "TOKEN_EXPIRED" else exc.category, exc.message)
                raise
            except Exception as exc:
                _store_inbox_failure(conn, comment_id, exc)
                raise ValueError("Chưa xác nhận được kết quả. Vui lòng đối chiếu trên Facebook.") from None
    return {"success": True, "reply_id": reply}


def get_report(days):
    end = datetime.now(timezone.utc)
    start = (end - timedelta(days=days)).isoformat()
    with closing(db.get_connection()) as conn:
        return [dict(row) for row in conn.execute("""
            SELECT p.page_name, p.page_id, t.status, COUNT(*) AS total
            FROM tasks t JOIN pages p ON p.page_id = t.page_id
            WHERE t.updated_at >= ?
            GROUP BY p.page_id, t.status ORDER BY p.page_name, t.status
        """, (start,))]


async def loop_publications():
    while True:
        try:
            await scheduler.run_worker(process_publications)
            scheduler.SCHEDULER_METRICS["publication_error"] = None
        except Exception:
            scheduler.SCHEDULER_METRICS["publication_error"] = "Không xử lý được lịch xuất bản."
        await asyncio.sleep(config.TASK_TICK_SEC)


async def loop_inbox():
    while True:
        try:
            await scheduler.run_worker(refresh_enabled_inboxes)
        except Exception:
            scheduler.SCHEDULER_METRICS.setdefault("inbox_errors", {})["system"] = "Không chạy được đồng bộ hộp thư."
        await asyncio.sleep(config.SHEET_SYNC_INTERVAL_SEC)


def rewrite_draft(text):
    model = config.GEMINI_MODEL.removeprefix("models/")
    if not config.GEMINI_API_KEY or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise ValueError("Cần cấu hình GEMINI_API_KEY và GEMINI_MODEL.")
    redacted = _phone.sub("[số điện thoại]", text)
    with httpx.Client(timeout=25, trust_env=False) as client:
        result = client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": config.GEMINI_API_KEY},
            json={
                "systemInstruction": {"parts": [{"text":
                    "Viết lại bản nháp tiếng Việt cho Fanpage. Giữ nguyên ý và dữ kiện. "
                    "Không thêm cam kết, không giả danh khách hàng. Chỉ trả bản nháp."
                }]},
                "contents": [{"parts": [{"text": redacted}]}],
            },
        )
        result.raise_for_status()
        candidates = result.json().get("candidates") or []
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        draft = "\n".join(part["text"] for part in parts if part.get("text") and not part.get("thought"))
        if not draft.strip():
            raise ValueError("Gemini chưa trả bản nháp.")
        return draft
