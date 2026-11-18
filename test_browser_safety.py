"""Persistent browser profile safeguards, using only isolated fixture databases."""
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

import browser_safety as safety
import config
import database as db
import submit_control


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


def test_begin_commits_marker_and_only_matching_finish_clears_it(safety_clock, monkeypatch):
    monkeypatch.setattr(config, "BROWSER_MIN_GAP_PER_PROFILE_SEC", 0)
    started = safety.begin_operation("shared", page_id="615000000000011", kind="send", task_id=7)
    assert started["ready"]
    stored = json.loads(db.get_setting("browser_safety:shared"))
    assert stored["version"] == 2
    assert stored["operation"]["operation_id"] == started["operation_id"]
    assert stored["operation"]["task_id"] == 7
    assert stored["operation"]["kind"] == "send"
    assert not safety.begin_operation("shared", kind="scan")["ready"]
    stale = safety.finish_operation("shared", "0" * 32, started["generation"])
    assert not stale["completed"]
    assert json.loads(db.get_setting("browser_safety:shared"))["operation"] is not None
    assert safety.finish_operation("shared", started["operation_id"], started["generation"])["completed"]
    assert json.loads(db.get_setting("browser_safety:shared"))["operation"] is None
    assert safety.status("shared")["count"] == 1


def test_begin_is_atomic_across_threads_even_without_gap(safety_clock, monkeypatch):
    monkeypatch.setattr(config, "BROWSER_MIN_GAP_PER_PROFILE_SEC", 0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: safety.begin_operation("shared", kind="scan"), range(4)))
    assert sum(bool(result["ready"]) for result in results) == 1
    assert safety.status("shared")["count"] == 1


def test_version_one_is_upgraded_without_resetting_history(safety_clock):
    old = {"version": 1, "day": "2026-11-15", "count": 2,
           "last_reserved_at": "2026-11-15T09:00:00+00:00", "browser_status": "CHECKPOINT",
           "generation": 4, "message": "Synthetic checkpoint"}
    db.set_setting("browser_safety:shared", json.dumps(old))
    check = safety.begin_operation("shared", kind="check", allow_blocked=True)
    assert check["ready"] and check["generation"] == 4 and check["count"] == 3
    stored = json.loads(db.get_setting("browser_safety:shared"))
    assert stored["version"] == 2 and stored["browser_status"] == "CHECKPOINT"
    assert safety.clear_block("shared", check["generation"], operation_id=check["operation_id"])["cleared"]
    assert safety.status("shared")["count"] == 3


def test_failed_begin_does_not_grant_browser_permission(safety_clock, monkeypatch):
    def fail_write(*args):
        raise sqlite3.OperationalError("synthetic write failure")
    monkeypatch.setattr(safety, "_write", fail_write)
    result = safety.begin_operation("shared", kind="send")
    assert not result["ready"] and result["safety_persistence_failed"]
    assert not db.get_setting("browser_safety:shared")
    assert safety.status("shared")["count"] == 0


def test_uncertain_begin_commit_is_detected_as_interruption(safety_clock, monkeypatch):
    real_connection = db.get_connection
    class UncertainCommit:
        def __init__(self):
            self.connection = real_connection()
        def __getattr__(self, name):
            return getattr(self.connection, name)
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            self.connection.__exit__(*exc)
            raise sqlite3.OperationalError("synthetic lost commit response")
    with monkeypatch.context() as patch:
        patch.setattr(db, "get_connection", UncertainCommit)
        started = safety.begin_operation("shared", kind="send")
    assert not started["ready"]
    assert safety.status("shared")["browser_status"] == "RECOVERY_REQUIRED"
    assert safety.recover_interrupted_operations() == 1
    assert not safety.begin_operation("shared", kind="send")["ready"]
    assert safety.status("shared")["count"] == 1


@pytest.mark.parametrize("status", ["LOGIN_REQUIRED", "CHECKPOINT", "RESTRICTED"])
def test_failed_block_preserves_evidence_and_requires_verified_recovery(safety_clock, monkeypatch, status):
    monkeypatch.setattr(config, "BROWSER_MIN_GAP_PER_PROFILE_SEC", 0)
    page = _page("615000000000011", "shared")
    pending = _task(page, "100")
    started = safety.begin_operation("shared", page_id=page, kind="send")
    real_write = safety._write
    def fail_write(*args):
        raise sqlite3.OperationalError("synthetic database full")
    with monkeypatch.context() as patch:
        patch.setattr(safety, "_write", fail_write)
        blocked = safety.block("shared", status, "Synthetic restriction", operation_id=started["operation_id"])
        assert blocked["browser_status"] == status and blocked["safety_persistence_failed"]
    assert safety._write is real_write
    assert safety.status("shared")["browser_status"] == status
    assert not safety.finish_operation("shared", started["operation_id"], started["generation"])["completed"]
    assert json.loads(db.get_setting("browser_safety:shared"))["operation"] is not None
    assert not safety.begin_operation("shared", kind="send")["ready"]
    check = safety.begin_operation("shared", page_id=page, kind="check", allow_blocked=True)
    assert check["ready"] and check["browser_status"] == status
    assert not safety.clear_block("shared", check["generation"])["cleared"]
    assert safety.clear_block("shared", check["generation"], operation_id=check["operation_id"])["cleared"]
    assert safety.status("shared")["browser_status"] == "READY"
    assert db.get_page(page)["browser_checked_at"] is None
    assert next(task for task in db.get_all_tasks(page_id=page) if task["id"] == pending["id"])["status"] == "PAUSED"


def test_failed_block_and_read_still_returns_original_evidence(safety_clock, monkeypatch):
    started = safety.begin_operation("shared", kind="send")
    def fail_connection():
        raise sqlite3.OperationalError("synthetic locked database")
    with monkeypatch.context() as patch:
        patch.setattr(db, "get_connection", fail_connection)
        result = safety.block("shared", "CHECKPOINT", "Synthetic checkpoint", operation_id=started["operation_id"])
        assert result["browser_status"] == "CHECKPOINT"
        assert safety.status("shared")["browser_status"] == "CHECKPOINT"
    assert safety.status("shared")["browser_status"] == "CHECKPOINT"


def test_cleanup_latch_cannot_replace_explicit_facebook_evidence(safety_clock, monkeypatch):
    safety.latch("shared", "RESTRICTED", "Synthetic Facebook restriction")
    safety.latch("shared", "RECOVERY_REQUIRED", "Synthetic cleanup failure")
    def fail_connection():
        raise sqlite3.OperationalError("synthetic database unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(db, "get_connection", fail_connection)
        assert safety.status("shared")["browser_status"] == "RESTRICTED"


def test_persisted_facebook_evidence_wins_over_generic_cleanup_latch(safety_clock):
    safety.block("shared", "CHECKPOINT", "Synthetic Facebook checkpoint")
    safety.latch("shared", "RECOVERY_REQUIRED", "Synthetic cleanup failure")
    assert safety.status("shared")["browser_status"] == "CHECKPOINT"
    check = safety.begin_operation("shared", kind="check", allow_blocked=True)
    assert check["ready"] and check["browser_status"] == "CHECKPOINT"
    assert safety.clear_block("shared", check["generation"], operation_id=check["operation_id"])["cleared"]


def test_status_can_share_the_short_policy_snapshot_connection(safety_clock, monkeypatch):
    safety.reserve_operation("shared")
    with closing(db.get_connection()) as connection:
        def fail_connection():
            raise AssertionError("must reuse caller snapshot")
        monkeypatch.setattr(db, "get_connection", fail_connection)
        assert safety.status("shared", conn=connection)["count"] == 1


def test_block_after_safety_snapshot_invalidates_only_its_profile_ticket(safety_clock, monkeypatch):
    ticket = submit_control.snapshot("615000000000011", profile_id="shared")
    independent = submit_control.snapshot("615000000000012", profile_id="other")
    graph = submit_control.snapshot("615000000000013")
    assert safety.status("shared")["browser_status"] == "READY"
    def fail_write(*args):
        raise sqlite3.OperationalError("synthetic block persistence failure")
    with monkeypatch.context() as patch:
        patch.setattr(safety, "_write", fail_write)
        blocked = safety.block("shared", "CHECKPOINT", "Synthetic checkpoint")
    assert blocked["safety_persistence_failed"]
    assert not submit_control.commit(ticket, lambda: False)
    assert submit_control.commit(independent, lambda: False)
    assert submit_control.commit(graph, lambda: False)


def test_failed_cleanup_keeps_marker_and_blocks_next_operation(safety_clock, monkeypatch):
    started = safety.begin_operation("shared", kind="send")
    def fail_write(*args):
        raise sqlite3.OperationalError("synthetic cleanup failure")
    with monkeypatch.context() as patch:
        patch.setattr(safety, "_write", fail_write)
        result = safety.finish_operation("shared", started["operation_id"], started["generation"])
    assert not result["completed"] and result["safety_persistence_failed"]
    assert json.loads(db.get_setting("browser_safety:shared"))["operation"] is not None
    assert safety.status("shared")["browser_status"] == "RECOVERY_REQUIRED"
    assert not safety.begin_operation("shared", kind="send")["ready"]


def test_late_block_and_finish_do_not_clear_a_newer_operation(safety_clock, monkeypatch):
    monkeypatch.setattr(config, "BROWSER_MIN_GAP_PER_PROFILE_SEC", 0)
    first = safety.begin_operation("shared", kind="scan")
    assert safety.finish_operation("shared", first["operation_id"], first["generation"])["completed"]
    current = safety.begin_operation("shared", kind="send")
    safety.block("shared", "RESTRICTED", "Synthetic late restriction", operation_id=first["operation_id"])
    assert not safety.finish_operation("shared", first["operation_id"], first["generation"])["completed"]
    assert not safety.finish_operation("shared", current["operation_id"], current["generation"])["completed"]
    marker = json.loads(db.get_setting("browser_safety:shared"))["operation"]
    assert marker["operation_id"] == current["operation_id"]
    assert safety.status("shared")["browser_status"] == "RESTRICTED"


def test_clear_requires_a_check_receipt_and_failed_clear_never_opens_profile(safety_clock, monkeypatch):
    safety.block("shared", "CHECKPOINT", "Synthetic checkpoint")
    generation = safety.status("shared")["generation"]
    assert safety.clear_block("shared", generation)["reason"] == "CHECK_REQUIRED"
    check = safety.begin_operation("shared", kind="check", allow_blocked=True)
    def fail_write(*args):
        raise sqlite3.OperationalError("synthetic clear failure")
    with monkeypatch.context() as patch:
        patch.setattr(safety, "_write", fail_write)
        cleared = safety.clear_block("shared", check["generation"], operation_id=check["operation_id"])
    assert not cleared["cleared"] and cleared["safety_persistence_failed"]
    assert not safety.status("shared")["ready"]
    assert safety.status("shared")["browser_status"] == "CHECKPOINT"
    assert json.loads(db.get_setting("browser_safety:shared"))["operation"] is not None


def test_real_child_crash_is_quarantined_before_workers(safety_clock):
    page = _page("615000000000011", "shared")
    pending = _task(page, "100")
    sending = _task(page, "101")
    with closing(db.get_connection()) as conn, conn:
        conn.execute("UPDATE tasks SET status='SENDING' WHERE id=?", (sending["id"],))
    child = """
import os, sys
from pathlib import Path
from datetime import datetime, timezone
import database as db
import browser_safety as safety
db.DB_PATH = Path(sys.argv[1])
fixed = datetime.fromisoformat(sys.argv[2])
class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
db.datetime = safety.datetime = Clock
assert safety.begin_operation('shared', page_id='615000000000011', kind='send')['ready']
os._exit(0)
"""
    child_env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "PATH") if name in os.environ}
    child_env.update(PYTHON_DOTENV_DISABLED="1", ENCRYPTION_KEY=Fernet.generate_key().decode(),
                     DASHBOARD_ADMIN_KEY="synthetic-dashboard-key-for-child-0000")
    completed = subprocess.run([sys.executable, "-c", child, str(db.DB_PATH), safety_clock.now(timezone.utc).isoformat()],
                               cwd=os.path.dirname(os.path.abspath(__file__)), env=child_env,
                               capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    assert safety.status("shared")["browser_status"] == "RECOVERY_REQUIRED"
    assert safety.recover_interrupted_operations() == 1
    assert safety.recover_interrupted_operations() == 0
    assert db.get_page(page)["browser_checked_at"] is None
    states = {task["id"]: task["status"] for task in db.get_all_tasks(page_id=page)}
    assert states[pending["id"]] == "PAUSED"
    assert states[sending["id"]] == "SENDING"
    assert safety.status("shared")["count"] == 1


def test_startup_reconciliation_failure_is_not_silenced(safety_clock, monkeypatch):
    operation = safety.begin_operation("shared", kind="scan")
    safety.abandon_operation("shared", operation["operation_id"])
    def fail_write(*args):
        raise sqlite3.OperationalError("synthetic startup persistence failure")
    monkeypatch.setattr(safety, "_write", fail_write)
    with pytest.raises(sqlite3.OperationalError):
        safety.recover_interrupted_operations()
    assert not safety.status("shared")["ready"]


@pytest.mark.parametrize("field,value", [("kind", "unknown"), ("owner_instance_id", "bad"),
    ("operation_id", None), ("generation", -1), ("started_at", "2026-11-15T10:00:00"), ("task_id", True)])
def test_malformed_operation_marker_fails_closed(safety_clock, field, value):
    safety.begin_operation("shared", kind="scan")
    stored = json.loads(db.get_setting("browser_safety:shared"))
    stored["operation"][field] = value
    db.set_setting("browser_safety:shared", json.dumps(stored))
    assert safety.status("shared")["reason"] == "SAFETY_STATE_INVALID"
    assert not safety.begin_operation("shared", kind="check", allow_blocked=True)["ready"]
    with pytest.raises(RuntimeError):
        safety.recover_interrupted_operations()


def test_scan_stops_at_send_reserve_while_send_and_check_remain_allowed(safety_clock, monkeypatch):
    monkeypatch.setattr(config, "BROWSER_MAX_OPERATIONS_PER_PROFILE_PER_DAY", 6)
    monkeypatch.setattr(config, "BROWSER_RESERVED_SEND_OPERATIONS", 2, raising=False)
    monkeypatch.setattr(config, "BROWSER_MIN_GAP_PER_PROFILE_SEC", 0)
    for _ in range(4):
        op = safety.begin_operation("shared", kind="scan")
        assert op["ready"]
        assert safety.finish_operation("shared", op["operation_id"], op["generation"])["completed"]
    denied_scan = safety.begin_operation("shared", kind="scan")
    assert not denied_scan["ready"]
    assert denied_scan["reason"] == "WAIT_PROFILE_SEND_RESERVE"
    assert safety.status("shared")["ready"] is True
    assert safety.status("shared")["scan_ready"] is False
    for kind in ("send", "check"):
        op = safety.begin_operation("shared", kind=kind)
        assert op["ready"]
        assert safety.finish_operation("shared", op["operation_id"], op["generation"])["completed"]
    exhausted = safety.begin_operation("shared", kind="send")
    assert not exhausted["ready"] and exhausted["reason"] == "WAIT_PROFILE_DAILY"
