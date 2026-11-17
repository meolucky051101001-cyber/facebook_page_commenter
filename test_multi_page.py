import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from urllib.parse import parse_qs, urlsplit

import app
import database as db
import meta_client
import migrations
import operations
import scheduler
import sheets_sync


PAGE_A = "100000000000001"
PAGE_B = "100000000000002"
HEADERS = ["ID trang", "Tên Fanpage", "Comment", "Số phút chờ sau khi đăng"]
URL = "https://docs.google.com/spreadsheets/d/mock/edit#gid=123"


@pytest.fixture
def pages(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "multi.sqlite3")
    monkeypatch.setitem(scheduler.SCHEDULER_METRICS, "paused_all", False)
    monkeypatch.setattr(scheduler, "_SCAN_BACKOFF_UNTIL", {})
    monkeypatch.setattr(scheduler, "_comment_sheet_result", {"success": True})
    monkeypatch.setattr(scheduler, "comment_sheet_url", lambda: URL)
    monkeypatch.setattr(scheduler, "refresh_comment_sheet", lambda **kwargs: {"success": True})
    db.init_db()
    for page_id, name in ((PAGE_A, "A"), (PAGE_B, "B")):
        db.upsert_page(page_id, name, "mock-" + name, db.upsert_account(name))
        assert not db.get_page(page_id)["rule_enabled"]
    result = sheets_sync.sync_comment_sheet_rows([HEADERS, [PAGE_A, "A", "A1", "10"], [PAGE_B, "B", "B1", "20"]])
    assert result["pages_synced"] == 2
    monkeypatch.setattr(meta_client, "verify_page_connection", lambda page_id, token: {
        "success": True, "token_info": {"status": "OK"},
    })
    for page_id in (PAGE_A, PAGE_B):
        assert app.set_page_automation(page_id, app.PageAutomationRequest(enabled=True))["enabled"]
    return db


def test_two_pages_dedupe_checkpoint_and_dispatch(pages, monkeypatch, clock):
    # Windows wall-clock reads can share the same tick as the second Page's
    # enable checkpoint. Model a post strictly after both activation cursors.
    clock.value = max(datetime.fromisoformat(pages.get_page(page_id)["last_seen_post_time"])
                      for page_id in (PAGE_A, PAGE_B)) + timedelta(seconds=1)
    old = (clock.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    new = clock.now(timezone.utc).isoformat()
    monkeypatch.setattr(meta_client, "get_recent_posts", lambda *args, **kwargs: [
        {"id": "old", "created_time": old}, {"id": "same-post", "created_time": new},
    ])
    scheduler.detect_new_posts_and_schedule()
    scheduler.detect_new_posts_and_schedule()
    tasks = pages.get_all_tasks()
    assert {task["page_id"]: task["comment_content"] for task in tasks} == {PAGE_A: "A1", PAGE_B: "B1"}
    assert len(tasks) == 2 and all(task["post_id"] == "same-post" for task in tasks)
    with pages.get_connection() as conn:
        conn.execute("UPDATE tasks SET scheduled_at = ?", (old,))
    sent = []
    monkeypatch.setattr(meta_client, "verify_post_belongs_to_page", lambda post, page, token: {"clean_post_id": post})

    def send(post, page, token, content):
        sent.append((page, token, content))
        if page == PAGE_A:
            return {"success": False, "category": "NEED_CHECK", "error": "Ambiguous response"}
        return {"success": True, "comment_id": "mock-comment"}

    monkeypatch.setattr(meta_client, "post_comment", send)
    scheduler.execute_due_tasks()
    scheduler.execute_due_tasks()
    assert sorted(sent) == [(PAGE_A, "mock-A", "A1"), (PAGE_B, "mock-B", "B1")]
    assert {task["page_id"]: task["status"] for task in pages.get_all_tasks()} == {
        PAGE_A: "NEED_CHECK", PAGE_B: "SUCCESS",
    }
    task_id = pages.get_all_tasks(page_id=PAGE_A)[0]["id"]
    assert not pages.retry_task(task_id)


def test_sheet_edit_isolates_error_and_updates_pending_auto(pages):
    created = datetime.now(timezone.utc)
    due = (created + timedelta(minutes=10)).isoformat()
    pages.create_task(PAGE_A, "a", "A1", due, post_created_time=created.isoformat())
    pages.create_task(PAGE_A, "manual", "manual", due, source="MANUAL")
    pages.create_task(PAGE_B, "b", "B1", due)
    result = sheets_sync.sync_comment_sheet_rows([
        HEADERS, [PAGE_A, "A", "A2", "30"], [PAGE_B, "B", "B2", "20"], [PAGE_B, "B", "B3", "21"],
    ], save_url=URL)
    assert result["pages_synced"] == result["pending_updated"] == 1
    assert pages.get_setting("comment_sheet_url") == URL
    assert pages.get_page(PAGE_B)["source_error"]
    assert all(pages.get_page(page)["rule_enabled"] for page in (PAGE_A, PAGE_B))
    assert pages.get_all_tasks(source="AUTO", page_id=PAGE_B)[0]["comment_content"] == "B1"
    task = pages.get_all_tasks(source="AUTO", page_id=PAGE_A)[0]
    assert task["comment_content"] == "A2"
    assert task["scheduled_at"] == (created + timedelta(minutes=30)).isoformat()
    assert pages.get_all_tasks(source="MANUAL")[0]["comment_content"] == "manual"
    result = app.set_page_automation(PAGE_A, app.PageAutomationRequest(enabled=False))
    assert result["cancelled_auto_tasks"] == 1
    assert pages.get_all_tasks(source="MANUAL")[0]["status"] == "PENDING"


def test_new_posts_only_survives_restart_and_reenable(clock, ready_pages, monkeypatch):
    page_id, other_page = ready_pages
    db.set_page_automation(other_page, False)
    enabled_at = clock.now(timezone.utc)
    posts = [
        {"id": "before-enable", "created_time": (enabled_at - timedelta(seconds=1)).isoformat()},
        {"id": "at-enable", "created_time": enabled_at.isoformat()},
        {"id": "new-post", "created_time": (enabled_at + timedelta(minutes=1)).isoformat()},
    ]
    scanned_since = []

    def recent_posts(page, token, *, since_timestamp):
        assert page == page_id
        scanned_since.append(since_timestamp)
        return posts

    monkeypatch.setattr(meta_client, "get_recent_posts", recent_posts)
    clock.advance(minutes=2)
    scheduler.detect_new_posts_and_schedule()
    tasks = db.get_all_tasks(page_id=page_id)
    assert [task["post_id"] for task in tasks] == ["new-post"]
    db.update_task_result(tasks[0]["id"], "SUCCESS", "existing-comment")
    checkpoint = db.get_page(page_id)["last_seen_post_time"]

    db.init_db()
    clock.advance(minutes=1)
    assert not db.set_page_automation(page_id, True)["changed"]
    assert db.get_page(page_id)["last_seen_post_time"] == checkpoint
    scheduler.detect_new_posts_and_schedule()
    assert scanned_since == [enabled_at.isoformat(), checkpoint]
    assert len(db.get_all_tasks(page_id=page_id)) == 1

    db.set_page_automation(page_id, False)
    clock.advance(minutes=1)
    posts.append({"id": "while-disabled", "created_time": clock.now(timezone.utc).isoformat()})
    clock.advance(minutes=1)
    resumed_at = clock.now(timezone.utc)
    assert db.set_page_automation(page_id, True)["changed"]
    clock.advance(minutes=1)
    posts.append({"id": "after-reenable", "created_time": clock.now(timezone.utc).isoformat()})
    scheduler.detect_new_posts_and_schedule()
    assert scanned_since[-1] == resumed_at.isoformat()
    assert {task["post_id"]: task["status"] for task in db.get_all_tasks(page_id=page_id)} == {
        "new-post": "SUCCESS", "after-reenable": "PENDING",
    }


def test_v2_migration_preserves_pilot_history_and_cursor(tmp_path):
    with sqlite3.connect(tmp_path / "v2.sqlite3") as conn:
        migrations._apply_migration_v1(conn)
        migrations._apply_migration_v2(conn)
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version VALUES (2)")
        for page in (PAGE_A, PAGE_B):
            conn.execute("INSERT INTO pages (page_id, page_name, page_access_token_enc, last_seen_post_time) VALUES (?, ?, '', 'cursor')",
                         (page, page))
            conn.execute("INSERT INTO page_rules (page_id, is_enabled) VALUES (?, 1)", (page,))
        conn.execute("INSERT INTO tasks (page_id,post_id,dedupe_key,comment_content,scheduled_at,status) VALUES (?, 'post', 'key', 'msg', 'time', 'SUCCESS')",
                     (PAGE_A,))
        migrations.run_migrations(conn, preserve_auto_page_id=PAGE_A)
        assert conn.execute("SELECT page_id,is_enabled FROM page_rules ORDER BY page_id").fetchall() == [(PAGE_A, 1), (PAGE_B, 0)]
        assert conn.execute("SELECT DISTINCT last_seen_post_time FROM pages").fetchall() == [("cursor",)]
        assert conn.execute("SELECT status FROM tasks").fetchall() == [("SUCCESS",)]
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_legacy_hours_and_three_templates(pages):
    result = sheets_sync.sync_comment_sheet_rows([
        ["ID trang", "Comment", "Tên Fanpage", "Số giờ chờ sau khi đăng bài"],
        [PAGE_A, "1", "A", "0.16666666666666666"], [PAGE_A, "2", "A", "1/6"], [PAGE_A, "3", "A", ""],
    ], legacy_page_id=PAGE_A)
    assert result["templates_count"] == 3
    assert pages.get_page(PAGE_A)["delay_minutes"] == 10
    assert pages.get_page(PAGE_B)["source_error"]


def test_sequence_uses_page_alias_and_waits_after_actual_send(pages, monkeypatch):
    alias = "615000000000001"
    with pages.get_connection() as conn:
        conn.execute("UPDATE pages SET fb_profile_id = ? WHERE page_id = ?", (alias, PAGE_A))
    result = sheets_sync.sync_comment_sheet_rows([
        HEADERS + ["Thứ tự"],
        [alias, "A", "A1", "30", "1"],
        [PAGE_A, "A", "A2", "35", "2"],
        [alias, "A", "A3", "40", "3"],
        [PAGE_B, "B", "B1", "30", ""],
    ])
    assert result["pages_synced"] == 2
    created = datetime.now(timezone.utc)
    monkeypatch.setattr(meta_client, "get_recent_posts", lambda *args, **kwargs: [
        {"id": "post-1", "created_time": created.isoformat()},
    ])
    scheduler.detect_new_posts_and_schedule()
    scheduler.detect_new_posts_and_schedule()
    tasks = sorted(pages.get_all_tasks(page_id=PAGE_A), key=lambda task: task["sequence_step"])
    assert [task["comment_content"] for task in tasks] == ["A1", "A2", "A3"]
    assert [task["scheduled_at"] for task in tasks] == [
        (created + timedelta(minutes=offset)).isoformat() for offset in (30, 35, 40)
    ]
    assert len(pages.get_all_tasks(page_id=PAGE_B)) == 1
    with pages.get_connection() as conn:
        conn.execute("UPDATE tasks SET scheduled_at = ?", (created.isoformat(),))
    assert [task["id"] for task in pages.get_due_tasks(page_id=PAGE_A)] == [tasks[0]["id"]]
    assert pages.claim_due_task(tasks[1]["id"], created.isoformat()) is None
    pages.update_task_result(tasks[0]["id"], "SUCCESS", "mock-comment")
    assert not pages.get_due_tasks(page_id=PAGE_A)
    second = pages.get_all_tasks(page_id=PAGE_A)[1]
    assert datetime.fromisoformat(second["scheduled_at"]) >= created + timedelta(minutes=5)
    assert pages.cancel_task(tasks[1]["id"])
    assert pages.get_all_tasks(page_id=PAGE_A)[0]["status"] == "CANCELLED"


def test_publication_queues_comment_once_on_verified_post(pages, monkeypatch):
    published = datetime.now(timezone.utc)
    entry = app.PublicationRequest(
        external_key="schedule-1", page_id=PAGE_A, caption="Nội dung",
        first_comment="Bình luận", comment_delay_minutes=10,
        scheduled_at=published - timedelta(seconds=1),
    )
    assert operations.add_publications([entry]) == 1
    assert operations.add_publications([entry]) == 0
    sent = []

    def fake_publish(*args):
        sent.append(args)
        return "100"

    monkeypatch.setattr(meta_client, "publish_page_post", fake_publish)
    monkeypatch.setattr(meta_client, "verify_post_belongs_to_page", lambda *args: {
        "clean_post_id": "100000000000001_100", "created_time": published.isoformat(),
    })
    operations.process_publications()
    operations.process_publications()
    assert len(sent) == 1
    assert operations.list_publications()[0]["status"] == "SUCCESS"
    tasks = pages.get_all_tasks(page_id=PAGE_A)
    assert len(tasks) == 1
    assert tasks[0]["comment_content"] == "Bình luận"
    assert tasks[0]["scheduled_at"] == (published + timedelta(minutes=10)).isoformat()


def test_inbox_masks_phone_and_avoids_duplicate_reply(pages, monkeypatch):
    monkeypatch.setattr(meta_client, "get_recent_posts", lambda *args, **kwargs: [{"id": "12_34"}])
    monkeypatch.setattr(meta_client, "page_request", lambda *args: {"data": [
        {"id": "12_35", "message": "Gọi 0912345678", "from": {"id": "34", "name": "Khách"}},
        {"id": "12_36", "message": "Page tự bình luận", "from": {"id": PAGE_A}},
    ]})
    assert operations.refresh_inbox(PAGE_A) == 1
    assert operations.refresh_inbox(PAGE_A) == 1
    assert [row["message"] for row in operations.list_inbox()] == ["Gọi [số điện thoại]"]
    monkeypatch.setattr(meta_client, "page_request", lambda *args: {"id": "12_37"})
    operations.moderate("12_35", "reply", "Cảm ơn bạn")
    with pytest.raises(ValueError):
        operations.moderate("12_35", "reply", "Cảm ơn bạn")


def test_automatic_moderation_applies_to_new_customer_comments_only(pages, monkeypatch):
    operations.set_moderation_rules(PAGE_A, True, "GIÁ", "Bạn xem giá trong bài nhé.")
    created = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    with pages.get_connection() as conn:
        conn.executemany("""
            INSERT INTO incoming_comments
                (comment_id, page_id, post_id, author_name, message, has_phone, created_at)
            VALUES (?, ?, '12_34', 'Khách', 'Xin giá [số điện thoại]', 1, ?)
        """, [("12_35", PAGE_A, created), ("12_36", PAGE_A, "2020-01-01T00:00:00+00:00")])
    actions = []

    def request(method, path, token, params):
        actions.append((path, params))
        return {"id": "12_37"} if path.endswith("/comments") else {"success": True}

    monkeypatch.setattr(meta_client, "page_request", request)
    operations.apply_moderation_rules(PAGE_A)
    operations.apply_moderation_rules(PAGE_A)
    assert actions == [
        ("12_35", {"is_hidden": "true"}),
        ("12_35/comments", {"message": "Bạn xem giá trong bài nhé."}),
    ]


def test_publication_without_override_uses_page_sequence(pages, monkeypatch):
    sheets_sync.sync_comment_sheet_rows([
        HEADERS + ["Thứ tự"],
        [PAGE_A, "A", "A1", "30", "1"],
        [PAGE_A, "A", "A2", "35", "2"],
    ])
    created = datetime.now(timezone.utc)
    post_id = f"{PAGE_A}_1234"
    operations.add_publications([app.PublicationRequest(
        external_key="sequence", page_id=PAGE_A, caption="Bài mới",
        scheduled_at=created - timedelta(seconds=1),
    )])
    monkeypatch.setattr(meta_client, "publish_page_post", lambda *args: post_id)
    monkeypatch.setattr(meta_client, "verify_post_belongs_to_page", lambda *args: {
        "clean_post_id": post_id, "created_time": created.isoformat(),
    })
    monkeypatch.setattr(meta_client, "get_recent_posts", lambda *args, **kwargs: [
        {"id": post_id, "created_time": created.isoformat()},
    ])
    operations.process_publications()
    scheduler.detect_new_posts_and_schedule()
    tasks = pages.get_all_tasks(page_id=PAGE_A)
    assert sorted((task["sequence_step"], task["comment_content"]) for task in tasks) == [(1, "A1"), (2, "A2")]


def test_operations_handlers_import_csv_and_store_settings(pages):
    html = app.operations_dashboard()
    assert html.status_code == 200 and "Lịch xuất bản" in html.body.decode()
    content = (
        "external_key,page_id,caption,scheduled_at\n"
        f"csv-test,{PAGE_A},Một bài mới,2030-01-01T10:00:00+07:00\n"
    )
    assert app.publication_import(app.PublicationCSV(content=content)) == {"inserted": 1}
    job = app.publication_list()["publications"][0]
    assert job["scheduled_at"] == "2030-01-01T03:00:00+00:00"
    assert app.publication_cancel(job["id"])["success"]
    assert app.report(days=7)["rows"] == []
    assert app.inbox_monitor(app.PageAutomationRequest(enabled=True))["enabled"]
    assert app.inbox_list()["monitor_enabled"]


def test_facebook_login_imports_account_pages_and_rejects_replay(pages, monkeypatch):
    callback = "http://localhost:8080/facebook/callback"
    monkeypatch.setattr(app.config, "META_APP_ID", "123")
    monkeypatch.setattr(app.config, "META_APP_SECRET", "mock-secret")
    monkeypatch.setattr(app.config, "META_OAUTH_REDIRECT_URI", callback)
    monkeypatch.setattr(app, "_facebook_logins", {})
    calls = []

    def exchange(code, redirect_uri):
        calls.append((code, redirect_uri))
        return "short-token"

    monkeypatch.setattr(meta_client, "exchange_facebook_login_code", exchange)
    monkeypatch.setattr(meta_client, "exchange_for_long_lived_user_token", lambda token: ("long-token", None))
    monkeypatch.setattr(meta_client, "get_user_info", lambda token: {"id": "1234", "name": "Chủ Page"})
    monkeypatch.setattr(meta_client, "get_user_pages", lambda token: [
        {"id": PAGE_A, "name": "A", "access_token": "new-page-a"},
        {"id": PAGE_B, "name": "B", "access_token": "new-page-b"},
    ])
    monkeypatch.setattr(meta_client, "debug_token", lambda token: {"checked": True, "status": "OK"})

    def request(path, cookie="", host=b"localhost:8080"):
        return Request({
            "type": "http", "scheme": "http", "path": path,
            "headers": [(b"host", host), (b"cookie", cookie.encode())],
            "router": app.app.router,
        })

    with pytest.raises(HTTPException) as error:
        app.facebook_login_start(request("/api/accounts/facebook/start", host=b"127.0.0.1:8080"),
                                 app.FacebookLoginRequest())
    assert error.value.status_code == 409
    response = app.facebook_login_start(request("/api/accounts/facebook/start"),
                                        app.FacebookLoginRequest(publish_access=True))
    login_url = urlsplit(json.loads(response.body)["url"])
    options = parse_qs(login_url.query)
    state = options["state"][0]
    cookie = response.headers["set-cookie"].split(";", 1)[0]
    assert options["redirect_uri"] == [callback]
    assert "pages_manage_posts" in options["scope"][0].split(",")
    no_cookie = app.facebook_login_callback(request("/facebook/callback"), state, "code")
    assert "invalid" in no_cookie.headers["location"]
    result = app.facebook_login_callback(request("/facebook/callback", cookie), state, "code")
    assert result.headers["location"] == "/?facebook_login=connected&pages=2"
    replay = app.facebook_login_callback(request("/facebook/callback", cookie), state, "code")
    assert "invalid" in replay.headers["location"]
    assert calls == [("code", callback)]
    assert pages.get_page_with_decrypted_token(PAGE_B)["page_access_token"] == "new-page-b"
