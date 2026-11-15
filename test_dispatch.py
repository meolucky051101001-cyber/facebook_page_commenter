from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest
import comment_dispatch
import database as db
import meta_client
import operations
import scheduler
import service_lock
from test_auto_comment_fixes import queue_due


def add_customer_comment(page_id, comment_id="12_35", created=None):
    created = created or db.datetime.now(timezone.utc).isoformat()
    with closing(db.get_connection()) as conn, conn:
        conn.execute("""INSERT INTO incoming_comments
            (comment_id,page_id,post_id,author_name,message,created_at)
            VALUES (?,?,'12_34','Guest','price',?)""", (comment_id, page_id, created))


@pytest.mark.parametrize("first", ["post", "reply"])
def test_concurrent_post_and_reply_share_last_daily_slot(ready_pages, monkeypatch, first):
    a, _ = ready_pages
    db.update_page_daily_limit(a, 1)
    queue_due(a, a + "_10")
    add_customer_comment(a)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def send(*args):
        calls.append(args)
        entered.set()
        assert release.wait(5), "Test failed to release mocked HTTP request"
        return {"success": True, "comment_id": "mock", "id": "mock"}

    monkeypatch.setattr(meta_client, "post_comment", send)
    monkeypatch.setattr(meta_client, "page_request", send)
    with ThreadPoolExecutor(max_workers=2) as pool:
        work = pool.submit(scheduler.execute_due_tasks if first == "post" else
                           lambda: operations.moderate("12_35", "reply", "Price"))
        try:
            assert entered.wait(5)
            if first == "post":
                assert operations.moderate("12_35", "reply", "Price")["reason"] == "WAIT_BUSY"
            else:
                scheduler.execute_due_tasks()
            assert len(calls) == 1
        finally:
            release.set()
        work.result(timeout=5)
    assert db.get_page(a)["comments_today_count"] == 1
    assert comment_dispatch.check(a, lambda: False).reason == "WAIT_DAILY"


def test_automatic_replies_wait_for_page_gap(ready_pages, monkeypatch, clock):
    a, _ = ready_pages
    operations.set_moderation_rules(a, False, "price", "Read the post")
    for i in range(3):
        add_customer_comment(a, f"12_{i}")
    calls = []
    monkeypatch.setattr(meta_client, "page_request", lambda *args: calls.append(args) or {"id": str(len(calls))})
    operations.apply_moderation_rules(a)
    assert len(calls) == 1
    clock.advance(seconds=44)
    operations.apply_moderation_rules(a)
    assert len(calls) == 1
    clock.advance(seconds=1)
    operations.apply_moderation_rules(a)
    assert len(calls) == 2
    assert db.get_page(a)["comments_today_count"] == 2


def test_automatic_reply_rechecks_rules_after_claim(ready_pages, monkeypatch):
    a, _ = ready_pages
    operations.set_moderation_rules(a, False, "price", "Old reply")
    add_customer_comment(a)
    original_check = comment_dispatch.check
    sends = []

    def check_after_rules_change(*args, **kwargs):
        with closing(db.get_connection()) as conn:
            pending = conn.execute("SELECT action_pending FROM incoming_comments WHERE comment_id='12_35'").fetchone()[0]
        if pending == "reply":
            operations.set_moderation_rules(a, False, "price", "New reply")
        return original_check(*args, **kwargs)

    monkeypatch.setattr(comment_dispatch, "check", check_after_rules_change)
    monkeypatch.setattr(meta_client, "page_request", lambda *args: sends.append(args) or {"id": "mock"})
    operations.apply_moderation_rules(a)
    assert sends == []
    item = operations.list_inbox(a)[0]
    assert item["reply_id"] is None and item["action_pending"] is None
    assert db.get_page(a)["comments_today_count"] == 0


def test_reconnect_during_verification_does_not_send_with_old_token(ready_pages, monkeypatch):
    a, _ = ready_pages
    queue_due(a, a + "_1")
    sends = []

    def verify(post, page, token):
        assert token == "mock-" + a
        db.upsert_page(a, "Page 1", "new-mock-token", db.get_page(a)["account_id"])
        return {"clean_post_id": post}

    monkeypatch.setattr(meta_client, "verify_post_belongs_to_page", verify)
    monkeypatch.setattr(meta_client, "post_comment", lambda *args: sends.append(args))
    scheduler.execute_due_tasks()
    assert sends == []
    assert db.get_all_tasks(page_id=a)[0]["status"] == "PENDING"
    assert db.get_page_with_decrypted_token(a)["page_access_token"] == "new-mock-token"


def test_rate_limit_persists_when_task_exhausts_retries(ready_pages, monkeypatch, clock):
    a, b = ready_pages
    queue_due(a, a + "_1")
    queue_due(a, a + "_2", minutes=1)
    queue_due(b, b + "_1", "Template B", minutes=1)
    with closing(db.get_connection()) as conn, conn:
        conn.execute("UPDATE tasks SET retry_count=4 WHERE post_id=?", (a + "_1",))
    calls = []

    def send(post, page, token, content):
        calls.append(page)
        return {"success": False, "category": "RATE_LIMIT", "error": "limited"} if page == a else {"success": True, "comment_id": "mock"}

    monkeypatch.setattr(meta_client, "post_comment", send)
    scheduler.execute_due_tasks()
    assert calls == [a, b]
    with closing(db.get_connection()) as conn:
        assert conn.execute("SELECT send_cooldown_until FROM pages WHERE page_id=?", (a,)).fetchone()[0]
    scheduler.SCHEDULER_METRICS["send_errors"].clear()
    scheduler.execute_due_tasks()
    assert calls == [a, b]
    assert db.get_all_tasks(page_id=a)[-1]["status"] == "FAILED"
    clock.advance(minutes=2)
    scheduler.execute_due_tasks()
    assert calls == [a, b, a]


def test_app_cooldown_and_retry_after_cover_other_pages(ready_pages, clock):
    a, b = ready_pages
    until = db.record_rate_limit(a, {"rate_limit_scope": "APP", "retry_after_seconds": 600})
    assert until == (clock.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    assert comment_dispatch.check(b, lambda: False).reason == "WAIT_RATE_LIMIT"
    clock.advance(minutes=10)
    assert comment_dispatch.check(b, lambda: False).ready


def test_success_and_manual_resolution_count_once(ready_pages):
    a, _ = ready_pages
    queue_due(a, a + "_1")
    task = db.get_all_tasks()[0]
    db.update_task_result(task["id"], "SUCCESS", "mock")
    db.update_task_result(task["id"], "SUCCESS", "mock")
    assert not db.resolve_task(task["id"])
    assert db.get_page(a)["comments_today_count"] == 1


def test_new_local_day_resets_quota_but_keeps_gap(ready_pages, clock):
    a, _ = ready_pages
    clock.value = clock.value.replace(hour=16, minute=59, second=50)
    db.update_page_daily_limit(a, 1)
    with closing(db.get_connection()) as conn, conn:
        db.record_comment_success(conn, a, clock.now(timezone.utc))
    assert comment_dispatch.check(a, lambda: False).reason == "WAIT_GAP"
    clock.advance(seconds=11)
    assert comment_dispatch.check(a, lambda: False).reason == "WAIT_GAP"
    clock.advance(seconds=34)
    assert comment_dispatch.check(a, lambda: False).ready


def test_database_lock_rejects_second_process_and_recovers_after_crash(tmp_path):
    path = tmp_path / "process.sqlite3"
    code = "import sys; from service_lock import exclusive_database; ctx=exclusive_database(sys.argv[1]); ctx.__enter__(); print('locked',flush=True); sys.stdin.read()"
    process = subprocess.Popen([sys.executable, "-c", code, str(path)], cwd=Path(__file__).parent,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(RuntimeError, match="Commenter"):
            with service_lock.exclusive_database(path):
                pytest.fail("Second process acquired a live lock")
    finally:
        process.terminate()
        process.communicate(timeout=5)
    with service_lock.exclusive_database(path):
        pass
