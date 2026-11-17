import pytest
import sqlite3

import comment_dispatch
import database as db
import meta_client
import scheduler
from test_auto_comment_fixes import queue_due


@pytest.mark.parametrize("error", [
    {"code": 368, "message": "Temporarily blocked for policies violation"},
    {"code": 1, "message": "We restrict certain activity to protect our community."},
    {"code": 1, "message": "Chúng tôi hạn chế một số hoạt động để bảo vệ cộng đồng của mình."},
])
def test_graph_activity_restrictions_are_not_unknown_or_network_retries(error):
    category, message = meta_client.classify_error(error)
    assert category == "RESTRICTED"
    assert "cần kiểm tra" in message


def test_graph_restriction_stops_remaining_page_tasks_without_affecting_other_page(ready_pages, monkeypatch):
    a, b = ready_pages
    queue_due(a, a + "_101", minutes=3)
    queue_due(a, a + "_102", minutes=2)
    queue_due(b, b + "_201", minutes=1)
    calls = []
    def post(post_id, page_id, *args):
        calls.append(page_id)
        if page_id == a:
            category, message = meta_client.classify_error({"code": 368, "message": "Policy blocked"})
            return {"success": False, "category": category, "error": message}
        return {"success": True, "comment_id": "mock-other-page"}
    monkeypatch.setattr(meta_client, "post_comment", post)
    scheduler.execute_due_tasks()
    assert calls == [a, b]
    assert db.get_page(a)["token_status"] == "RESTRICTED"
    assert {task["status"] for task in db.get_all_tasks(page_id=a)} == {"PAUSED"}
    assert not comment_dispatch.check(a, lambda: False).ready
    scheduler.execute_due_tasks()
    assert calls == [a, b]


def test_paused_maintenance_does_not_read_graph_or_sheet(ready_pages, monkeypatch):
    scheduler.SCHEDULER_METRICS["paused_all"] = True
    def forbidden(*args, **kwargs):
        pytest.fail("Paused maintenance contacted an external source")
    monkeypatch.setattr(meta_client, "debug_token", forbidden)
    monkeypatch.setattr(scheduler, "refresh_comment_sheet", forbidden)
    scheduler.run_maintenance_cycle()


@pytest.mark.parametrize("category", ["RESTRICTED", "TOKEN_EXPIRED", "MISSING_PERMISSIONS"])
def test_failed_page_quarantine_keeps_durable_write_marker_and_blocks_until_new_proof(ready_pages, monkeypatch, category):
    page_id = ready_pages[0]
    queue_due(page_id, page_id + "_101", minutes=3)
    queue_due(page_id, page_id + "_102", minutes=2)
    attempts = []
    monkeypatch.setattr(meta_client, "post_comment", lambda *args: attempts.append("POST") or {
        "success": False, "category": category, "error": "Synthetic fatal Graph response",
    })
    with monkeypatch.context() as failing:
        def fail_page_persistence(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic failed quarantine commit")
        failing.setattr(db, "update_page_status", fail_page_persistence)
        with pytest.raises(sqlite3.OperationalError):
            scheduler.execute_due_tasks()
    assert {task["status"] for task in db.get_all_tasks(page_id=page_id)} == {"SENDING", "PENDING"}
    assert comment_dispatch.graph_quarantined(page_id)
    scheduler.execute_due_tasks()
    assert attempts == ["POST"]
    # Model process memory loss. The persisted SENDING marker alone must force
    # fresh Page proof before any later write, while preserving NEED_CHECK.
    monkeypatch.setattr(comment_dispatch, "_graph_quarantine", set())
    db.recover_stale_tasks(startup=True)
    assert db.get_page(page_id)["connection_checked_at"] is None
    assert db.get_page(page_id)["token_status"] == "UNVERIFIED"
    assert {task["status"] for task in db.get_all_tasks(page_id=page_id)} == {"NEED_CHECK", "PAUSED"}
    assert not comment_dispatch.graph_ready(db.get_page_for_dispatch(page_id))
    scheduler.execute_due_tasks()
    assert attempts == ["POST"]
    db.update_page_connection_check(page_id, db.datetime.now(db.timezone.utc).isoformat(), token_status="OK")
    assert comment_dispatch.graph_ready(db.get_page_for_dispatch(page_id))
    assert {task["status"] for task in db.get_all_tasks(page_id=page_id)} == {"NEED_CHECK", "PAUSED"}
