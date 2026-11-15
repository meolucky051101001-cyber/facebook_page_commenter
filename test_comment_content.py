from contextlib import closing
from datetime import datetime, timedelta, timezone
import json

import pytest
import app
import comment_content
import database as db
import meta_client
import scheduler
import sheets_sync
from test_auto_comment_fixes import HEADERS, queue_due


def test_rendered_text_survives_unchanged_sync_and_retry(ready_pages):
    a, _ = ready_pages
    rows = [HEADERS, [a, "A", "{Hello|Hi} {page_name}", "10"]]
    sheets_sync.sync_comment_sheet_rows(rows)
    queue_due(a, a + "_1")
    sheets_sync.sync_comment_sheet_rows(rows)
    before = db.get_all_tasks()[0]
    assert before["template_source"] == "{Hello|Hi} {page_name}"
    assert sheets_sync.sync_comment_sheet_rows(rows)["pending_updated"] == 0
    db.update_task_result(before["id"], "FAILED", error_message="Not transmitted")
    assert db.retry_task(before["id"])
    after = db.get_all_tasks()[0]
    assert (after["comment_content"], after["content_revision"]) == (before["comment_content"], before["content_revision"])


def test_page_name_is_literal_and_nested_spintax_is_supported():
    assert comment_content.render("{Hello|{Hi|Welcome}} {page_name}", "Shop {A|B}", "fixed") in (
        "Hello Shop {A|B}", "Hi Shop {A|B}", "Welcome Shop {A|B}")
    assert comment_content.render("A | B", "Page", "fixed") == "A | B"


@pytest.mark.parametrize("template", ["{unknown_variable}", "{A|}", "{ |A}", "{A|B", "x" * 8001])
def test_invalid_template_only_blocks_its_page(ready_pages, template):
    a, b = ready_pages
    result = sheets_sync.sync_comment_sheet_rows([HEADERS, [a, "A", template, "10"], [b, "B", "Valid", "10"]])
    assert result["success"] and result["pages_synced"] == 1
    assert db.get_page(a)["source_error"]
    assert db.get_page(b)["source_error"] is None


@pytest.mark.parametrize("batch", [False, True])
def test_resume_forces_sheet_read_and_keeps_future_delay(ready_pages, monkeypatch, batch):
    a, _ = ready_pages
    queue_due(a, a + "_1")
    task = db.get_all_tasks()[0]
    db.update_page_status(a, True, "EXPIRED")
    db.update_page_status(a, True, "OK")
    calls = []

    def refresh(**kwargs):
        calls.append(kwargs)
        return sheets_sync.sync_comment_sheet_rows([HEADERS, [a, "A", "New price 200", "30"]])

    monkeypatch.setattr(scheduler, "refresh_comment_sheet", refresh)
    if batch:
        app.resume_page_tasks(a)
    else:
        app.retry_task(task["id"])
    updated = db.get_all_tasks()[0]
    assert calls == [{"force": True}]
    assert updated["comment_content"] == "New price 200"
    assert updated["status"] == "PENDING"
    assert datetime.fromisoformat(updated["scheduled_at"]) > datetime.now(timezone.utc)


def test_sheet_failure_leaves_resumed_task_paused(ready_pages, monkeypatch):
    a, _ = ready_pages
    queue_due(a, a + "_1")
    task = db.get_all_tasks()[0]
    db.update_task_result(task["id"], "PAUSED")
    monkeypatch.setattr(scheduler, "refresh_comment_sheet", lambda **kw: {"success": False, "error": "Sheet offline"})
    with pytest.raises(app.HTTPException, match="Sheet offline"):
        app.retry_task(task["id"])
    assert db.get_all_tasks()[0]["status"] == "PAUSED"


def test_claim_uses_latest_rule_and_sending_content_is_frozen(ready_pages):
    a, _ = ready_pages
    queue_due(a, a + "_1")
    stale = db.get_due_tasks()[0]
    sheets_sync.sync_comment_sheet_rows([HEADERS, [a, "A", "New", "10"]])
    claimed = db.claim_due_task(stale["id"], datetime.now(timezone.utc).isoformat())
    assert claimed["comment_content"] == "New"
    sheets_sync.sync_comment_sheet_rows([HEADERS, [a, "A", "Newer", "10"]])
    assert db.get_all_tasks()[0]["comment_content"] == "New"


def test_sheet_change_during_verification_keeps_claimed_content(ready_pages, monkeypatch):
    a, _ = ready_pages
    queue_due(a, a + "_1", "Old price 100")
    sheets_sync.sync_comment_sheet_rows([HEADERS, [a, "A", "Claimed price 200", "10"]])
    sends = []

    def verify(post, page, token):
        sheets_sync.sync_comment_sheet_rows([HEADERS, [a, "A", "Future price 300", "10"]])
        return {"clean_post_id": post}

    monkeypatch.setattr(meta_client, "verify_post_belongs_to_page", verify)
    monkeypatch.setattr(meta_client, "post_comment", lambda *args: sends.append(args[3]) or
                        {"success": True, "comment_id": "mock"})
    scheduler.execute_due_tasks()
    assert sends == ["Claimed price 200"]
    assert db.get_all_tasks(page_id=a)[0]["comment_content"] == "Claimed price 200"
    assert json.loads(db.get_page(a)["templates"]) == ["Future price 300"]


def test_sequence_snapshot_and_single_mode_change_are_preserved(ready_pages):
    a, _ = ready_pages
    queue_due(a, a + "_old")
    sheets_sync.sync_comment_sheet_rows([HEADERS, [a, "A", "Old single", "10"]])
    old = db.get_all_tasks()[0]
    rows = [HEADERS + ["Thứ tự"], [a, "A", "One", "10", "1"], [a, "A", "Two", "15", "2"]]
    sheets_sync.sync_comment_sheet_rows(rows)
    now = datetime.now(timezone.utc)
    post = scheduler.build_post_schedule(db.get_page(a), a + "_sequence", None, now, now)
    with closing(db.get_connection()) as conn, conn:
        db.insert_auto_post_tasks(conn, a, post)
    sheets_sync.sync_comment_sheet_rows([HEADERS, [a, "A", "New single", "20"]])
    tasks = db.get_all_tasks(page_id=a)
    assert {task["sequence_step"]: task["comment_content"] for task in tasks if task["sequence_step"]} == {1: "One", 2: "Two"}
    assert next(task for task in tasks if task["id"] == old["id"])["comment_content"] == "Old single"


def test_need_check_is_immutable_until_confirmed_not_posted(ready_pages, monkeypatch):
    a, _ = ready_pages
    queue_due(a, a + "_1", "Previously attempted")
    task = db.get_all_tasks()[0]
    db.update_task_result(task["id"], "NEED_CHECK")
    sheets_sync.sync_comment_sheet_rows([HEADERS, [a, "A", "New text", "10"]])
    assert db.get_all_tasks()[0]["comment_content"] == "Previously attempted"
    with pytest.raises(ValueError):
        scheduler.resume_tasks(task_id=task["id"])
    assert scheduler.resume_tasks(task_id=task["id"], confirmed_not_posted=True) == 1
    assert db.get_all_tasks()[0]["comment_content"] == "New text"


def test_resuming_very_old_task_still_requires_review(ready_pages, monkeypatch):
    a, _ = ready_pages
    queue_due(a, a + "_1", minutes=180)
    task = db.get_all_tasks()[0]
    db.update_task_result(task["id"], "PAUSED")
    app.resume_page_tasks(a)
    calls = []
    monkeypatch.setattr(meta_client, "post_comment", lambda *args: calls.append(args))
    scheduler.execute_due_tasks()
    assert calls == []
    assert db.get_all_tasks()[0]["status"] == "NEED_CHECK"


@pytest.mark.parametrize("status", ["FAILED", "NEED_CHECK"])
def test_retry_recomputes_new_delay_after_source_changed(ready_pages, status):
    a, _ = ready_pages
    queue_due(a, a + "_1")
    sheets_sync.sync_comment_sheet_rows([HEADERS, [a,"A","Old","10"]])
    task = db.get_all_tasks()[0]
    db.update_task_result(task["id"], status)
    sheets_sync.sync_comment_sheet_rows([HEADERS, [a,"A","New","30"]])
    scheduler.resume_tasks(task_id=task["id"], confirmed_not_posted=True)
    updated = db.get_all_tasks()[0]
    assert updated["comment_content"] == "New"
    assert datetime.fromisoformat(updated["scheduled_at"]) > datetime.now(timezone.utc)
