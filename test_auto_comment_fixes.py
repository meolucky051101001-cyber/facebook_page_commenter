import json
from datetime import datetime, timedelta, timezone

import database as db
import meta_client
import operations
import scheduler
import sheets_sync


HEADERS = ["ID trang", "Tên Fanpage", "Comment", "Số phút chờ sau khi đăng"]


def queue_due(page_id, post, content="Template A", minutes=2):
    now = db.datetime.now(timezone.utc)
    db.create_task(page_id, post, content, (now - timedelta(minutes=minutes)).isoformat(),
                   post_created_time=(now - timedelta(minutes=minutes + 10)).isoformat())


def test_invalid_token_does_not_starve_other_pages(ready_pages, monkeypatch):
    a, b = ready_pages
    for number in range(10):
        queue_due(a, f"{a}_{number}", minutes=3)
    queue_due(b, f"{b}_101", "Template B", minutes=1)
    with db.get_connection() as conn:
        conn.execute("UPDATE pages SET page_access_token_enc='broken-token' WHERE page_id=?", (a,))
    sends = []
    monkeypatch.setattr(meta_client, "post_comment", lambda *args: sends.append(args[1]) or
                        {"success": True, "comment_id": "mock-comment"})
    scheduler.execute_due_tasks()
    assert sends == [b]
    assert db.get_page(a)["token_status"] == "INVALID_SECRET"
    assert {task["status"] for task in db.get_all_tasks(page_id=a)} == {"PAUSED"}


def test_old_decryption_failure_cannot_overwrite_reconnected_token(ready_pages):
    a, _ = ready_pages
    before = db.get_page_with_decrypted_token(a)["page_access_token_enc"]
    db.upsert_page(a, "Page 1", "new-mock-token", db.get_page(a)["account_id"])
    assert not db.invalidate_page_token(a, before)
    assert db.get_page(a)["token_status"] == "OK"
    assert db.get_page_with_decrypted_token(a)["page_access_token"] == "new-mock-token"


def test_auto_replies_respect_exhausted_daily_limit(ready_pages, monkeypatch):
    a, _ = ready_pages
    operations.set_moderation_rules(a, False, "price", "Read the post")
    now = datetime.now(timezone.utc)
    with db.get_connection() as conn:
        conn.execute("UPDATE page_rules SET max_comments_per_day=1 WHERE page_id=?", (a,))
        conn.execute("UPDATE pages SET comments_today_count=1, comments_today_date=? WHERE page_id=?",
                     (db.local_day_key(now), a))
        conn.executemany("""INSERT INTO incoming_comments
            (comment_id,page_id,post_id,author_name,message,created_at)
            VALUES (?,?,'12_34','Guest','price',?)""",
            [(f"12_{i}", a, (now + timedelta(seconds=1)).isoformat()) for i in range(3)])
    sends = []
    monkeypatch.setattr(meta_client, "page_request", lambda *args: sends.append(args) or {"id": "reply-id"})
    operations.apply_moderation_rules(a)
    assert sends == []
    assert db.get_page(a)["comments_today_count"] == 1
    assert all(item["action_pending"] is None for item in operations.list_inbox(a))


def test_single_comment_renders_spintax_and_page_name(ready_pages):
    a, _ = ready_pages
    sheets_sync.sync_comment_sheet_rows([HEADERS, [a, "A", "{Hello|Hi} {page_name}", "10"]])
    now = datetime.now(timezone.utc)
    schedule = scheduler.build_post_schedule(db.get_page(a), a + "_123", None, now, now)
    assert schedule["steps"][0]["content"] in ("Hello Page 1", "Hi Page 1")


def test_rate_limit_stops_the_remaining_page_batch(ready_pages, monkeypatch):
    a, _ = ready_pages
    for number in range(3):
        queue_due(a, f"{a}_{number}")
    sends = []
    monkeypatch.setattr(meta_client, "post_comment", lambda *args: sends.append(args) or
                        {"success": False, "category": "RATE_LIMIT", "error": "limited"})
    scheduler.execute_due_tasks()
    scheduler.execute_due_tasks()
    assert len(sends) == 1
    assert sum(task["retry_count"] for task in db.get_all_tasks(page_id=a)) == 1


def test_paused_single_task_uses_updated_sheet_on_resume(ready_pages, monkeypatch):
    a, _ = ready_pages
    sheets_sync.sync_comment_sheet_rows([HEADERS, [a, "A", "OLD price 100", "10"]])
    queue_due(a, a + "_123", "OLD price 100")
    db.update_page_status(a, True, "EXPIRED")
    sheets_sync.sync_comment_sheet_rows([HEADERS, [a, "A", "NEW price 200", "10"]])
    assert db.get_all_tasks(page_id=a)[0]["status"] == "PAUSED"
    db.update_page_status(a, True, "OK")
    db.resume_paused_tasks_for_page(a)
    sends = []
    monkeypatch.setattr(meta_client, "post_comment", lambda *args: sends.append(args[3]) or
                        {"success": True, "comment_id": "mock-comment"})
    scheduler.execute_due_tasks()
    assert json.loads(db.get_page(a)["templates"]) == ["NEW price 200"]
    assert sends == ["NEW price 200"]
