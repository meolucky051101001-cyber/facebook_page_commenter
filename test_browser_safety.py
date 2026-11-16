"""Persistent browser profile safeguards, using only isolated fixture databases."""
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone

import pytest

import browser_safety as safety
import config
import database as db


@pytest.fixture
def safety_clock(clock, monkeypatch):
    monkeypatch.setattr(safety, "datetime", clock)
    monkeypatch.setattr(config, "BROWSER_MAX_OPERATIONS_PER_PROFILE_PER_DAY", 3, raising=False)
    monkeypatch.setattr(config, "BROWSER_MIN_GAP_PER_PROFILE_SEC", 60, raising=False)
    return clock


def test_budget_is_shared_persistent_and_status_does_not_reserve(safety_clock):
    assert safety.status("shared")["ready"]
    assert safety.status("shared")["count"] == 0
    first = safety.reserve_operation("shared")
    assert first["ready"] and first["count"] == 1
    denied = safety.reserve_operation("shared")
    assert not denied["ready"] and denied["reason"] == "WAIT_PROFILE_GAP"
    assert safety.status("shared")["count"] == 1
    assert safety.reserve_operation("other")["ready"]
    db.init_db()
    assert safety.status("shared")["count"] == 1
    assert safety.status("shared")["reason"] == "WAIT_PROFILE_GAP"
    for _ in range(2):
        safety_clock.advance(seconds=60)
        assert safety.reserve_operation("shared")["ready"]
    safety_clock.advance(seconds=60)
    denied = safety.reserve_operation("shared")
    assert denied["reason"] == "WAIT_PROFILE_DAILY"
    assert denied["count"] == 3
    assert denied["retry_at"] == db.next_daily_reset_utc(safety_clock.now(timezone.utc))


def test_daily_rollover_preserves_gap(safety_clock):
    # Local midnight is 17:00 UTC with the application's default UTC+7 offset.
    safety_clock.value = datetime(2026, 11, 15, 16, 59, 45, tzinfo=timezone.utc)
    assert safety.reserve_operation("shared")["ready"]
    safety_clock.advance(seconds=20)
    current = safety.status("shared")
    assert current["count"] == 0 and current["reason"] == "WAIT_PROFILE_GAP"
    assert not safety.reserve_operation("shared")["ready"]
    safety_clock.advance(seconds=40)
    assert safety.reserve_operation("shared")["count"] == 1


def test_daily_limit_and_gap_wait_for_the_later_deadline(safety_clock, monkeypatch):
    monkeypatch.setattr(config, "BROWSER_MAX_OPERATIONS_PER_PROFILE_PER_DAY", 1)
    safety_clock.value = datetime(2026, 11, 15, 16, 59, 45, tzinfo=timezone.utc)
    assert safety.reserve_operation("shared")["ready"]
    for result in (safety.status("shared"), safety.reserve_operation("shared")):
        assert result["retry_at"] == "2026-11-15T17:00:45+00:00"
        assert result["reason"] == "WAIT_PROFILE_GAP"


def test_reservations_are_atomic_across_threads(safety_clock):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: safety.reserve_operation("shared"), range(4)))
    assert sum(bool(result["ready"]) for result in results) == 1
    assert safety.status("shared")["count"] == 1


def test_daily_limit_is_atomic_without_a_gap(safety_clock, monkeypatch):
    monkeypatch.setattr(config, "BROWSER_MAX_OPERATIONS_PER_PROFILE_PER_DAY", 1)
    monkeypatch.setattr(config, "BROWSER_MIN_GAP_PER_PROFILE_SEC", 0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: safety.reserve_operation("shared"), range(4)))
    assert sum(bool(result["ready"]) for result in results) == 1
    assert sum(result["reason"] == "WAIT_PROFILE_DAILY" for result in results) == 3


def _page(pid, profile, mode="BROWSER"):
    account = db.upsert_account("Synthetic " + pid)
    db.upsert_antidetect_page(pid, "Synthetic Page", f"https://www.facebook.com/{pid}",
                              profile, "Synthetic Profile", account)
    if mode == "BROWSER":
        db.set_page_send_mode(pid, mode)
        assert db.update_page_browser_check(pid, browser_status="READY", identity_id=pid, profile_id=profile)
    return pid


def _task(pid, suffix):
    assert db.create_task(pid, f"{pid}_{suffix}", "Synthetic comment", "2026-11-15T00:00:00+00:00", source="MANUAL")
    return next(task for task in db.get_all_tasks(page_id=pid) if task["post_id"] == f"{pid}_{suffix}")


@pytest.mark.parametrize("blocked_status", ["LOGIN_REQUIRED", "CHECKPOINT", "RESTRICTED"])
def test_block_is_profile_wide_and_preserves_uncertain_writes(safety_clock, blocked_status):
    a = _page("615000000000011", "shared")
    b = _page("615000000000012", "shared")
    graph = _page("615000000000013", "shared", "GRAPH_API")
    other = _page("615000000000014", "other")
    tasks = {pid: _task(pid, "100") for pid in (a, b, graph, other)}
    active = _task(a, "101")
    with closing(db.get_connection()) as conn, conn:
        conn.execute("UPDATE tasks SET status='SENDING' WHERE id=?", (active["id"],))
    safety.block("shared", blocked_status, "Synthetic restriction")
    for pid in (a, b):
        page = db.get_page(pid)
        assert page["browser_status"] == blocked_status
        assert page["browser_checked_at"] is None
        assert page["browser_identity_id"] is None
    with closing(db.get_connection()) as conn:
        states = dict(conn.execute("SELECT id,status FROM tasks"))
    assert states[tasks[a]["id"]] == states[tasks[b]["id"]] == "PAUSED"
    assert states[active["id"]] == "SENDING"
    assert states[tasks[graph]["id"]] == states[tasks[other]["id"]] == "PENDING"
    assert db.get_page(other)["browser_status"] == "READY"
    assert db.get_page(graph)["send_mode"] == "GRAPH_API"
    assert not safety.reserve_operation("shared")["ready"]
    db.init_db()
    assert safety.status("shared")["browser_status"] == blocked_status


def test_recovery_requires_current_generation_and_does_not_resume_pages(safety_clock):
    page = _page("615000000000011", "shared")
    pending = _task(page, "100")
    first = safety.reserve_operation("shared")
    safety.block("shared", "CHECKPOINT", "Synthetic checkpoint")
    assert not safety.clear_block("shared", first["generation"])["cleared"]
    safety_clock.advance(seconds=60)
    check = safety.reserve_operation("shared", allow_blocked=True)
    assert check["ready"] and check["browser_status"] == "CHECKPOINT"
    safety.block("shared", "RESTRICTED", "Newer restriction")
    assert not safety.clear_block("shared", check["generation"])["cleared"]
    safety_clock.advance(seconds=60)
    check = safety.reserve_operation("shared", allow_blocked=True)
    assert safety.clear_block("shared", check["generation"])["cleared"]
    assert safety.status("shared")["browser_status"] == "READY"
    assert db.get_page(page)["browser_status"] == "RESTRICTED"
    assert db.get_all_tasks(page_id=page)[0]["status"] == "PAUSED"
    assert safety.status("shared")["count"] == 3


@pytest.mark.parametrize("bad", ["", "{", "[]", "{}", json.dumps({"version": 1, "day": "bad", "count": 0,
    "last_reserved_at": None, "browser_status": "READY", "generation": 0})])
def test_malformed_state_fails_closed_even_for_recovery(safety_clock, bad):
    db.set_setting("browser_safety:shared", bad)
    assert safety.status("shared")["reason"] == "SAFETY_STATE_INVALID"
    assert safety.reserve_operation("shared", allow_blocked=True)["reason"] == "SAFETY_STATE_INVALID"
    assert not safety.clear_block("shared", 0)["cleared"]
    safety.block("shared", "CHECKPOINT", "Synthetic checkpoint")
    assert safety.status("shared")["reason"] == "SAFETY_STATE_INVALID"


@pytest.mark.parametrize("field,value", [("count", True), ("count", -1), ("generation", True),
    ("last_reserved_at", "2026-11-15T10:00:00"), ("last_reserved_at", "9999-12-31T23:59:59+00:00"),
    ("day", "2099-01-01"), ("version", True), ("browser_status", "UNKNOWN")])
def test_malformed_fields_fail_closed(safety_clock, field, value):
    safety.reserve_operation("shared")
    stored = json.loads(db.get_setting("browser_safety:shared"))
    stored[field] = value
    db.set_setting("browser_safety:shared", json.dumps(stored))
    assert safety.reserve_operation("shared", allow_blocked=True)["reason"] == "SAFETY_STATE_INVALID"


def test_a_fresh_successful_check_can_confirm_ready_generation(safety_clock):
    check = safety.reserve_operation("shared", allow_blocked=True)
    cleared = safety.clear_block("shared", check["generation"])
    assert cleared["cleared"]
    assert cleared["generation"] > check["generation"]
    assert not safety.clear_block("shared", check["generation"])["cleared"]
