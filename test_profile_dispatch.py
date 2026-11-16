"""Profile safety through scheduler and dashboard boundaries, without live I/O."""
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import antidetect_client as browser
import app
import browser_safety as safety
import config
import database as db
import meta_client
import scheduler


def _make_browser(pid, profile):
    db.upsert_antidetect_page(pid, "Synthetic Page", f"https://www.facebook.com/{pid}",
                              profile, "Synthetic Profile", db.upsert_account("Synthetic " + pid))
    db.set_page_send_mode(pid, "BROWSER")
    assert db.update_page_browser_check(pid, browser_status="READY", identity_id=pid, profile_id=profile)
    return pid


@pytest.fixture
def profile_pages(ready_pages):
    a, graph = ready_pages
    _make_browser(a, "shared")
    b = _make_browser("615000000000012", "shared")
    other = _make_browser("615000000000013", "other")
    return a, b, other, graph


@pytest.fixture
def client():
    # Database setup is provided by isolated_runtime; do not start background
    # service workers while exercising the HTTP policy boundary.
    client = TestClient(app.app, base_url="http://localhost", headers={"X-Dashboard-Key": config.DASHBOARD_ADMIN_KEY})
    try:
        yield client
    finally:
        client.close()


def _queue(pid, suffix, minutes=1):
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    assert db.create_task(pid, f"{pid}_{suffix}", "Synthetic comment", when, source="MANUAL")
    return next(task for task in db.get_all_tasks(page_id=pid) if task["post_id"] == f"{pid}_{suffix}")


def _task(task_id):
    return next(task for task in db.get_all_tasks() if task["id"] == task_id)


@pytest.mark.parametrize("blocked_status", ["CHECKPOINT", "RESTRICTED"])
@pytest.mark.parametrize("after_enter", [False, True])
def test_profile_stop_quarantines_siblings_but_other_transports_continue(profile_pages, monkeypatch, blocked_status, after_enter):
    a, b, other, graph = profile_pages
    first = _queue(a, "100", minutes=5)
    sibling = _queue(b, "101", minutes=4)
    independent = _queue(other, "102", minutes=3)
    graph_task = _queue(graph, "103", minutes=2)
    browser_calls = []
    graph_calls = []

    def send(page, post, content, *, before_submit, **kwargs):
        assert before_submit()
        browser_calls.append(page["page_id"])
        if page["page_id"] == a:
            return {"success": False, "category": "NEED_CHECK" if after_enter else "TOKEN_EXPIRED",
                    "browser_status": blocked_status, "error": "Synthetic Facebook restriction"}
        return {"success": True, "comment_id": "synthetic-browser-result"}

    monkeypatch.setattr(browser, "send_page_comment_cdp", send)
    monkeypatch.setattr(meta_client, "post_comment", lambda post, page, *args: graph_calls.append(page) or {
        "success": True, "comment_id": "synthetic-graph-result"})
    scheduler.execute_due_tasks()
    assert browser_calls == [a, other]
    assert graph_calls == [graph]
    assert _task(first["id"])["status"] == ("NEED_CHECK" if after_enter else "PAUSED")
    assert _task(sibling["id"])["status"] == "PAUSED"
    assert _task(independent["id"])["status"] == _task(graph_task["id"])["status"] == "SUCCESS"
    assert safety.status("shared")["browser_status"] == blocked_status
    assert db.get_page(a)["comments_today_count"] == db.get_page(b)["comments_today_count"] == 0
    assert db.get_page(other)["browser_status"] == "READY"
    scheduler.execute_due_tasks()
    assert browser_calls == [a, other]
    db.init_db()
    scheduler.execute_due_tasks()
    assert browser_calls == [a, other]
    assert safety.status("shared")["browser_status"] == blocked_status
    assert _task(first["id"])["status"] == ("NEED_CHECK" if after_enter else "PAUSED")


def test_limit_result_defers_without_retry_or_comment_quota(profile_pages, monkeypatch):
    a, _, _, _ = profile_pages
    pending = _queue(a, "100")
    retry_at = (datetime.now(timezone.utc) + timedelta(minutes=4)).isoformat()
    monkeypatch.setattr(browser, "send_page_comment_cdp", lambda *args, **kwargs: {
        "success": False, "category": "BROWSER_LIMIT", "browser_status": "READY",
        "retry_at": retry_at, "error": "Synthetic profile budget"})
    scheduler.execute_due_tasks()
    task = _task(pending["id"])
    assert task["status"] == "PENDING"
    assert task["scheduled_at"] == retry_at
    assert task["locked_at"] is None and task["retry_count"] == 0
    assert db.get_page(a)["browser_status"] == "READY"
    assert db.get_page(a)["comments_today_count"] == 0


def test_profile_budget_stops_before_sender_and_does_not_starve_graph(profile_pages, monkeypatch):
    a, b, _, graph = profile_pages
    monkeypatch.setattr(config, "BROWSER_MAX_OPERATIONS_PER_PROFILE_PER_DAY", 1)
    assert safety.reserve_operation("shared")["ready"]
    first = _queue(a, "100", minutes=5)
    second = _queue(b, "101", minutes=4)
    graph_task = _queue(graph, "102", minutes=3)
    monkeypatch.setattr(browser, "send_page_comment_cdp", lambda *args, **kwargs: pytest.fail("Exhausted profile must not launch sender"))
    monkeypatch.setattr(meta_client, "post_comment", lambda *args, **kwargs: {"success": True, "comment_id": "synthetic-result"})
    scheduler.execute_due_tasks()
    for task_id in (first["id"], second["id"]):
        task = _task(task_id)
        assert task["status"] == "PENDING" and task["retry_count"] == 0
        assert task["scheduled_at"] == db.next_daily_reset_utc(datetime.now(timezone.utc))
    assert _task(graph_task["id"])["status"] == "SUCCESS"
    assert safety.status("shared")["count"] == 1


def test_cannot_enable_auto_while_profile_blocked(client, profile_pages):
    a, b, _, _ = profile_pages
    assert client.post(f"/api/pages/{a}/automation", json={"enabled": False}).status_code == 200
    safety.block("shared", "CHECKPOINT", "Synthetic checkpoint")
    result = client.post(f"/api/pages/{a}/automation", json={"enabled": True})
    assert result.status_code == 409
    assert not db.get_page(a)["rule_enabled"]
    assert db.get_page(b)["browser_status"] == "CHECKPOINT"


@pytest.mark.parametrize("category", ["BROWSER_LIMIT", "BROWSER_BUSY"])
def test_limited_connection_check_preserves_ready_proof_and_tasks(client, profile_pages, monkeypatch, category):
    a, _, _, _ = profile_pages
    pending = _queue(a, "100")
    before = db.get_page(a)
    monkeypatch.setattr(browser, "verify_page_connection_cdp", lambda page: {
        "success": False, "category": category, "browser_status": "READY", "error": "Synthetic wait",
        "profile_id": "shared", "identity_id": None})
    result = client.post(f"/api/pages/{a}/check")
    assert result.status_code == 409
    after = db.get_page(a)
    for key in ("browser_status", "browser_checked_at", "browser_profile_id", "browser_identity_id", "browser_error"):
        assert after[key] == before[key]
    assert _task(pending["id"])["status"] == "PENDING"


def test_failed_dashboard_check_blocks_sibling_profile_pages(client, profile_pages, monkeypatch):
    a, b, other, _ = profile_pages
    pending = _queue(b, "100")
    monkeypatch.setattr(browser, "verify_page_connection_cdp", lambda page: {
        "success": False, "category": "TOKEN_EXPIRED", "browser_status": "RESTRICTED",
        "error": "Synthetic restriction", "profile_id": "shared", "identity_id": None})
    assert client.post(f"/api/pages/{a}/check").status_code == 409
    assert db.get_page(b)["browser_status"] == "RESTRICTED"
    assert _task(pending["id"])["status"] == "PAUSED"
    assert db.get_page(other)["browser_status"] == "READY"


def test_late_ready_check_cannot_override_newer_profile_block(client, profile_pages, monkeypatch):
    a, b, _, _ = profile_pages

    def stale_check(page):
        safety.block("shared", "RESTRICTED", "Restriction arrived during the check")
        return {"success": True, "browser_status": "READY", "profile_id": "shared", "identity_id": a}

    monkeypatch.setattr(browser, "verify_page_connection_cdp", stale_check)
    assert client.post(f"/api/pages/{a}/check").status_code == 409
    assert db.get_page(a)["browser_status"] == db.get_page(b)["browser_status"] == "RESTRICTED"


def test_shared_profile_scans_rotate_across_empty_results_failures_and_restart(clock, profile_pages, monkeypatch):
    a, b, _, graph = profile_pages
    for pid in (a, b):
        db.update_page_rule(pid, 10, ["Synthetic comment"], 20, True)
    cursor = clock.now(timezone.utc).isoformat()
    with closing(db.get_connection()) as conn, conn:
        conn.execute("UPDATE pages SET last_seen_post_time=? WHERE page_id IN (?,?)", (cursor, a, b))
    # Use fixed source order so success requires persistence of attempts, not an
    # incidental change of Page updated_at or its content cursor.
    original_get_pages = db.get_all_pages
    monkeypatch.setattr(db, "get_all_pages", lambda: sorted(original_get_pages(), key=lambda p: p["page_id"]))
    monkeypatch.setattr(config, "BROWSER_MIN_GAP_PER_PROFILE_SEC", 60)
    monkeypatch.setattr(config, "BROWSER_MAX_OPERATIONS_PER_PROFILE_PER_DAY", 100)
    actual = []
    graph_scans = []

    def scan(page, since_timestamp):
        reservation = safety.reserve_operation(page["antidetect_profile_id"])
        if not reservation["ready"]:
            raise browser.BrowserAutomationError("Synthetic profile gap", "BROWSER_LIMIT", "READY",
                                                  retry_at=reservation["retry_at"])
        actual.append(page["page_id"])
        assert since_timestamp == cursor
        if len(actual) == 1:
            raise browser.BrowserAutomationError("Synthetic read failure", "NETWORK_RETRYABLE", "READY")
        return []

    monkeypatch.setattr(browser, "get_recent_posts_for_page_cdp", scan)
    monkeypatch.setattr(meta_client, "get_recent_posts", lambda page, *args, **kwargs: graph_scans.append(page) or [])
    scheduler.detect_new_posts_and_schedule()
    assert actual == [a]
    assert not db.get_setting(scheduler._BROWSER_SCAN_ATTEMPT_PREFIX + b)
    clock.advance(minutes=4)
    scheduler.detect_new_posts_and_schedule()
    assert actual == [a, b]
    # Startup may clear process caches, but the next fair turn stays in SQLite.
    db.init_db()
    monkeypatch.setattr(scheduler, "_SCAN_BACKOFF_UNTIL", {})
    clock.advance(minutes=4)
    scheduler.detect_new_posts_and_schedule()
    clock.advance(minutes=4)
    scheduler.detect_new_posts_and_schedule()
    assert actual == [a, b, a, b]
    assert graph_scans == [graph] * 4
    assert safety.status("shared")["count"] == 4
    assert db.get_page(a)["last_seen_post_time"] == db.get_page(b)["last_seen_post_time"] == cursor


@pytest.mark.parametrize("category", ["BROWSER_BUSY", "BROWSER_LIMIT", "BROWSER_BLOCKED"])
def test_denied_scan_does_not_spend_fair_turn(profile_pages, monkeypatch, category):
    a, _, _, _ = profile_pages

    def denied(*args, **kwargs):
        raise browser.BrowserAutomationError("Synthetic denial", category, "READY")

    monkeypatch.setattr(browser, "get_recent_posts_for_page_cdp", denied)
    with pytest.raises(browser.BrowserAutomationError):
        scheduler._scan_browser_posts(db.get_page(a), "2026-11-15T00:00:00+00:00")
    assert not db.get_setting(scheduler._BROWSER_SCAN_ATTEMPT_PREFIX + a)


def test_final_callback_can_use_last_reserved_daily_slot(profile_pages, monkeypatch):
    a, _, _, _ = profile_pages
    monkeypatch.setattr(config, "BROWSER_MAX_OPERATIONS_PER_PROFILE_PER_DAY", 1)
    monkeypatch.setattr(config, "BROWSER_MIN_GAP_PER_PROFILE_SEC", 60)
    pending = _queue(a, "200")
    sends = []

    def send(page, *args, before_submit, **kwargs):
        assert safety.reserve_operation(page["antidetect_profile_id"])["ready"]
        assert not safety.status(page["antidetect_profile_id"])["ready"]
        assert before_submit()
        sends.append(True)
        return {"success": True, "comment_id": "synthetic-result"}

    monkeypatch.setattr(browser, "send_page_comment_cdp", send)
    scheduler.execute_due_tasks()
    assert sends == [True]
    assert _task(pending["id"])["status"] == "SUCCESS"
    assert safety.status("shared")["count"] == 1
