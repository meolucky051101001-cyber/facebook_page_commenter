"""Selected browser transport uses the same task policy without real browsers or HTTP."""
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

import antidetect_client as browser
import comment_dispatch
import database as db
import meta_client
import scheduler
import sheets_sync
from test_auto_comment_fixes import HEADERS, queue_due


@pytest.fixture
def browser_page(ready_pages, monkeypatch):
    page_id, _ = ready_pages
    with closing(db.get_connection()) as conn, conn:
        conn.execute("""UPDATE pages SET antidetect_profile_id='profile-1',fb_profile_id=?,
            page_access_token_enc='',token_status='ANTIDETECT_READY' WHERE page_id=?""", (page_id, page_id))
    db.set_page_send_mode(page_id, "BROWSER")
    assert db.update_page_browser_check(page_id, datetime.now(timezone.utc).isoformat(), "READY",
                                        identity_id=page_id, profile_id="profile-1")

    def unexpected_browser_preflight(*args, **kwargs):
        raise AssertionError("Browser dispatch must verify and send in one tab")

    monkeypatch.setattr(browser, "verify_post_for_page_cdp", unexpected_browser_preflight)

    def unexpected(*args, **kwargs):
        raise AssertionError("Selected BROWSER mode must never call Graph")

    monkeypatch.setattr(meta_client, "post_comment", unexpected)
    monkeypatch.setattr(meta_client, "get_recent_posts", unexpected)
    monkeypatch.setattr(meta_client, "verify_post_belongs_to_page", unexpected)
    return page_id


def assert_task(page_id, status, retry_count=0):
    task = db.get_all_tasks(page_id=page_id)[0]
    assert task["status"] == status
    assert task["retry_count"] == retry_count
    return task


def test_browser_task_sends_without_graph_token(browser_page, monkeypatch):
    queue_due(browser_page, browser_page + "_123")
    calls = []

    def send(page, post, content, *, post_url, before_submit):
        assert before_submit()
        calls.append((page["antidetect_profile_id"], post, content, post_url))
        return {"success": True, "comment_id": "123_456"}

    monkeypatch.setattr(browser, "send_page_comment_cdp", send)
    scheduler.execute_due_tasks()
    assert len(calls) == 1
    assert calls[0][:3] == ("profile-1", browser_page + "_123", "Template A")
    assert assert_task(browser_page, "SUCCESS")["comment_id"] == "123_456"
    page = db.get_page(browser_page)
    assert page["comments_today_count"] == 1
    assert page["token_status"] == "ANTIDETECT_READY"
    assert page["browser_status"] == "READY"


def test_browser_scanner_uses_verified_route_and_exact_timestamps(browser_page, monkeypatch, clock):
    checkpoint = (clock.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    with closing(db.get_connection()) as conn, conn:
        conn.execute("UPDATE pages SET last_seen_post_time=? WHERE page_id=?", (checkpoint, browser_page))
    calls = []

    def posts(page, since_timestamp):
        calls.append((page["page_id"], since_timestamp))
        return [
            {"id": "unknown-time", "created_time": None},
            {"id": "future", "created_time": (clock.now(timezone.utc) + timedelta(minutes=1)).isoformat()},
            {"id": "old", "created_time": checkpoint},
            {"id": browser_page + "_123", "created_time": (clock.now(timezone.utc) - timedelta(minutes=2)).isoformat()},
        ]

    monkeypatch.setattr(browser, "get_recent_posts_for_page_cdp", posts)
    scheduler.detect_new_posts_and_schedule()
    assert calls == [(browser_page, checkpoint)]
    tasks = db.get_all_tasks(page_id=browser_page)
    assert len(tasks) == 1 and tasks[0]["post_id"] == browser_page + "_123"


@pytest.mark.parametrize("change", ["pause", "profile", "identity", "mode", "quota", "disable"])
def test_browser_final_callback_stops_changed_policy(browser_page, monkeypatch, change):
    queue_due(browser_page, browser_page + "_123")
    submitted = []

    def send(page, post, content, *, post_url, before_submit):
        if change == "pause":
            scheduler.SCHEDULER_METRICS["paused_all"] = True
        elif change == "disable":
            db.set_page_automation(browser_page, False)
        else:
            with closing(db.get_connection()) as conn, conn:
                if change == "profile":
                    conn.execute("UPDATE pages SET antidetect_profile_id='changed' WHERE page_id=?", (browser_page,))
                elif change == "identity":
                    conn.execute("UPDATE pages SET fb_profile_id='999999999999999' WHERE page_id=?", (browser_page,))
                elif change == "mode":
                    conn.execute("UPDATE pages SET send_mode='GRAPH_API' WHERE page_id=?", (browser_page,))
                elif change == "quota":
                    conn.execute("UPDATE pages SET comments_today_count=20,comments_today_date=? WHERE page_id=?",
                                 (db.local_day_key(datetime.now(timezone.utc)), browser_page))
        if before_submit():
            submitted.append(True)
            return {"success": True, "comment_id": "unexpected"}
        return {"success": False, "category": "CANCELLED_BEFORE_SEND", "error": "policy changed"}

    monkeypatch.setattr(browser, "send_page_comment_cdp", send)
    scheduler.execute_due_tasks()
    assert submitted == []
    assert db.get_all_tasks(page_id=browser_page)[0]["status"] in ("PENDING", "PAUSED", "CANCELLED")
    assert db.get_page(browser_page)["comments_today_count"] == (20 if change == "quota" else 0)


def test_browser_refreshes_sheet_before_enter_and_reprepares_changed_single(browser_page, monkeypatch, clock):
    queue_due(browser_page, browser_page + "_123")
    sent = []
    refreshes = []

    def refresh(**kwargs):
        refreshes.append(kwargs.get("force", False))
        if kwargs.get("force"):
            sheets_sync.sync_comment_sheet_rows([HEADERS, [browser_page, "A", "Updated comment", "10"]])
        return {"success": True}

    def send(page, post, content, *, post_url, before_submit):
        if not before_submit():
            return {"success": False, "category": "CANCELLED_BEFORE_SEND"}
        sent.append(content)
        return {"success": True, "comment_id": "123_456"}

    monkeypatch.setattr(scheduler, "refresh_comment_sheet", refresh)
    monkeypatch.setattr(browser, "send_page_comment_cdp", send)
    scheduler.execute_due_tasks()
    assert sent == [] and True in refreshes
    assert_task(browser_page, "PENDING")
    clock.advance(seconds=30)
    scheduler.execute_due_tasks()
    assert sent == ["Updated comment"]
    assert_task(browser_page, "SUCCESS")


def test_browser_busy_preserves_retry_and_quota(browser_page, monkeypatch):
    queue_due(browser_page, browser_page + "_123")

    def busy(*args, **kwargs):
        raise browser.BrowserAutomationError("Profile đang được dùng.", "BROWSER_BUSY", "READY")

    monkeypatch.setattr(browser, "send_page_comment_cdp", busy)
    scheduler.execute_due_tasks()
    task = assert_task(browser_page, "PENDING")
    assert task["locked_at"] is None
    assert datetime.fromisoformat(task["scheduled_at"]) > datetime.now(timezone.utc)
    assert db.get_page(browser_page)["comments_today_count"] == 0
    assert db.get_page(browser_page)["browser_status"] == "READY"


def test_busy_profile_cannot_starve_other_pages(browser_page, ready_pages, monkeypatch):
    other_page = ready_pages[1]
    for index in range(12):
        queue_due(browser_page, browser_page + "_" + str(100 + index), minutes=3)
    queue_due(other_page, other_page + "_123", minutes=1)

    def busy(*args, **kwargs):
        raise browser.BrowserAutomationError("Profile đang được dùng.", "BROWSER_BUSY", "READY")

    calls = []
    monkeypatch.setattr(browser, "send_page_comment_cdp", busy)
    monkeypatch.setattr(meta_client, "verify_post_belongs_to_page", lambda post, *args: {"clean_post_id": post})
    monkeypatch.setattr(meta_client, "post_comment", lambda post, page, *args: calls.append(page) or {
        "success": True, "comment_id": "123_456",
    })
    scheduler.execute_due_tasks()
    assert calls == [other_page]
    assert {task["status"] for task in db.get_all_tasks(page_id=browser_page)} == {"PENDING"}
    assert db.get_page(browser_page)["comments_today_count"] == 0


@pytest.mark.parametrize("status", ["LOGIN_REQUIRED", "CHECKPOINT"])
def test_browser_auth_failure_pauses_browser_only(browser_page, monkeypatch, status):
    queue_due(browser_page, browser_page + "_123")
    queue_due(browser_page, browser_page + "_124")

    def auth(*args, **kwargs):
        raise browser.BrowserAutomationError("Cần kiểm tra profile.", "TOKEN_EXPIRED", status)

    monkeypatch.setattr(browser, "send_page_comment_cdp", auth)
    scheduler.execute_due_tasks()
    assert {task["status"] for task in db.get_all_tasks(page_id=browser_page)} == {"PAUSED"}
    page = db.get_page(browser_page)
    assert page["browser_status"] == status and page["token_status"] == "ANTIDETECT_READY"
    assert page["comments_today_count"] == 0


@pytest.mark.parametrize("result", [
    {"success": False, "category": "NEED_CHECK", "error": "No confirmation"},
    {"success": True, "comment_id": None},
])
def test_browser_ambiguous_submit_never_retries_automatically(browser_page, monkeypatch, result):
    queue_due(browser_page, browser_page + "_123")
    calls = []

    def send(*args, before_submit, **kwargs):
        assert before_submit()
        calls.append(True)
        return result.copy()

    monkeypatch.setattr(browser, "send_page_comment_cdp", send)
    scheduler.execute_due_tasks()
    scheduler.execute_due_tasks()
    task = assert_task(browser_page, "NEED_CHECK")
    assert calls == [True] and not db.retry_task(task["id"])
    assert db.get_page(browser_page)["comments_today_count"] == 0


def test_maintenance_keeps_browser_ready_despite_expired_graph(browser_page, monkeypatch):
    queue_due(browser_page, browser_page + "_123")
    db.upsert_page(browser_page, "Page 1", "expired-graph-token", db.get_page(browser_page)["account_id"])
    calls = []
    monkeypatch.setattr(meta_client, "debug_token", lambda token: calls.append(token) or {
        "checked": True, "is_valid": False,
    })
    scheduler.run_maintenance_cycle()
    assert "expired-graph-token" not in calls
    assert db.get_page(browser_page)["browser_status"] == "READY"
    assert_task(browser_page, "PENDING")


def test_browser_resume_requires_browser_verification_without_graph(browser_page):
    queue_due(browser_page, browser_page + "_123")
    db.invalidate_page_browser(browser_page, "LOGIN_REQUIRED", "Sign in")
    with pytest.raises(ValueError, match="chế độ gửi"):
        scheduler.resume_tasks(page_id=browser_page)
    assert db.update_page_browser_check(browser_page, browser_status="READY",
                                       identity_id=browser_page, profile_id="profile-1")
    assert scheduler.resume_tasks(page_id=browser_page) == 1
    assert_task(browser_page, "PENDING")
    assert comment_dispatch.check(browser_page, lambda: False, transport="selected").ready
    assert not comment_dispatch.check(browser_page, lambda: False).ready
