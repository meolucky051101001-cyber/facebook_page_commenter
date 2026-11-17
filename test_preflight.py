"""Read-only operational readiness from synthetic, isolated runtime metadata."""
from contextlib import closing
from datetime import timezone
import importlib
import json

import pytest

import antidetect_client
import browser_safety as safety
import config
import crypto
import database as db
import meta_client
import scheduler


A = "615000000000011"
B = "615000000000012"
GRAPH = "615000000000013"
OTHER = "615000000000014"


@pytest.fixture
def preflight_clock(clock, monkeypatch):
    monkeypatch.setattr(config, "BROWSER_MAX_OPERATIONS_PER_PROFILE_PER_DAY", 3)
    monkeypatch.setattr(config, "BROWSER_MIN_GAP_PER_PROFILE_SEC", 60)
    return clock


def _page(pid, profile="shared", *, mode="BROWSER", checked=True, enabled=True):
    account_id = db.upsert_account("Synthetic account " + pid)
    if mode == "GRAPH_API":
        db.upsert_page(pid, "Synthetic " + pid[-3:], "synthetic-graph-credential", account_id)
    db.upsert_antidetect_page(pid, "Synthetic " + pid[-3:],
                              "https://www.facebook.com/" + pid, profile,
                              "Synthetic " + profile, account_id)
    if mode == "BROWSER":
        db.set_page_send_mode(pid, mode)
        if checked:
            assert db.update_page_browser_check(pid, browser_status="READY",
                                                 identity_id=pid, profile_id=profile)
    elif checked:
        db.update_page_connection_check(pid, "2026-11-15T10:00:00+00:00", token_status="OK")
    db.set_page_automation(pid, enabled)
    with closing(db.get_connection()) as conn, conn:
        conn.execute("UPDATE page_rules SET source_synced_at=?,source_error=NULL,templates=? WHERE page_id=?",
                     ("2026-11-15T10:00:00+00:00", '["Synthetic comment"]', pid))
    return pid


def _snapshot(paused=True):
    readiness = importlib.import_module("operational_readiness")
    return readiness.snapshot(paused_all=paused, sheet_sync={
        "sheet_url": "https://docs.google.com/spreadsheets/d/synthetic/edit",
        "last_run": "2026-11-15T10:00:00+00:00", "last_error": None,
    })


def _profiles(result):
    return {profile["profile_id"]: profile for profile in result["profile_safety"]["profiles"]}


def _dump():
    with closing(db.get_connection()) as conn:
        return tuple(conn.iterdump())


@pytest.mark.parametrize("status", ["LOGIN_REQUIRED", "CHECKPOINT", "RESTRICTED"])
def test_preflight_block_is_shared_only_by_selected_browser_pages(preflight_clock, status):
    _page(A)
    _page(B)
    _page(GRAPH, mode="GRAPH_API")
    _page(OTHER, "other")
    safety.block("shared", status, "Synthetic operator action required")

    result = _snapshot()
    profiles = _profiles(result)
    assert profiles["shared"]["status"] == "BLOCKED"
    assert set(profiles["shared"]["blocked_page_ids"]) == {A, B}
    assert profiles["shared"]["page_count"] == 2
    assert profiles["other"]["status"] == "READY"
    blocked_ids = {row["page_id"] for row in result["profile_safety"]["blocked_pages"]}
    assert {A, B} <= blocked_ids
    assert GRAPH not in blocked_ids and OTHER not in blocked_ids
    assert result["preflight"]["blocked_profiles"] == 1


def test_preflight_observes_memory_latch_without_persisting_or_resuming(preflight_clock):
    _page(A)
    _page(B)
    safety.latch("shared", "RESTRICTED", "Synthetic restriction before persistence")
    before = _dump()
    result = _snapshot()
    profile = _profiles(result)["shared"]
    assert profile["status"] == "BLOCKED"
    assert set(profile["blocked_page_ids"]) == {A, B}
    assert _dump() == before
    assert safety.status("shared")["browser_status"] == "RESTRICTED"


def test_preflight_invalid_safety_state_has_unknown_count_and_fails_closed(preflight_clock):
    _page(A)
    db.set_setting("browser_safety:shared", "{malformed")
    before = _dump()
    result = _snapshot()
    profile = _profiles(result)["shared"]
    assert profile["status"] == "BLOCKED"
    assert profile["operations_today"] is None
    assert A in profile["blocked_page_ids"]
    assert result["preflight"]["eligible_pages"] == 0
    assert _dump() == before


def test_preflight_distinguishes_profile_gap_from_invalid_page_proof(preflight_clock):
    _page(A)
    _page(B)
    assert safety.reserve_operation("shared")["ready"]
    result = _snapshot()
    profile = _profiles(result)["shared"]
    assert profile["status"] == "WAITING"
    assert profile["reason"] == "WAIT_PROFILE_GAP"
    assert profile["operations_today"] == 1
    assert profile["operations_limit"] == 3
    assert profile["retry_at"]
    assert set(profile["verified_page_ids"]) == {A, B}
    assert db.get_page(A)["browser_status"] == "READY"
    assert db.get_page(B)["browser_status"] == "READY"


def test_preflight_daily_budget_wait_has_reset_time_and_does_not_spend_more(preflight_clock):
    _page(A)
    for _ in range(3):
        assert safety.reserve_operation("shared")["ready"]
        preflight_clock.advance(seconds=60)
    result = _snapshot()
    profile = _profiles(result)["shared"]
    assert profile["status"] == "WAITING"
    assert profile["reason"] == "WAIT_PROFILE_DAILY"
    assert profile["operations_today"] == 3
    assert profile["retry_at"] == db.next_daily_reset_utc(preflight_clock.now(timezone.utc))
    assert safety.status("shared")["count"] == 3


def test_one_verified_page_does_not_verify_its_sibling(preflight_clock):
    _page(A)
    _page(B, checked=False)
    result = _snapshot()
    profile = _profiles(result)["shared"]
    assert profile["status"] == "READY"
    assert set(profile["verified_page_ids"]) == {A}
    assert B in {row["page_id"] for row in result["profile_safety"]["pages_needing_attention"]}
    assert db.get_page(B)["browser_status"] == "UNCHECKED"
    assert db.get_page(B)["browser_checked_at"] is None


def test_missing_profile_is_reported_without_creating_a_ready_profile(preflight_clock):
    _page(A)
    with closing(db.get_connection()) as conn, conn:
        conn.execute("UPDATE pages SET antidetect_profile_id=NULL WHERE page_id=?", (A,))
    result = _snapshot()
    assert not any(not item["profile_id"] and item["status"] == "READY"
                   for item in result["profile_safety"]["profiles"])
    assert A in {row["page_id"] for row in result["profile_safety"]["pages_needing_attention"]}
    assert result["preflight"]["eligible_pages"] == 0


def test_preflight_needs_check_count_covers_older_than_latest_500(preflight_clock):
    _page(A)
    with closing(db.get_connection()) as conn, conn:
        conn.executemany("""INSERT INTO tasks
            (page_id,post_id,dedupe_key,source,comment_content,scheduled_at,status)
            VALUES (?,?,?,'MANUAL','Synthetic content','2026-11-15T10:00:00+00:00',?)""", [
                (A, str(index), "synthetic-history-" + str(index), "NEED_CHECK" if index == 0 else "SUCCESS")
                for index in range(502)
            ])
    assert not any(task["status"] == "NEED_CHECK" for task in db.get_all_tasks(limit=500))
    result = _snapshot()
    assert result["preflight"]["needs_check_count"] == 1
    assert result["preflight"]["task_counts"]["NEED_CHECK"] == 1
    assert result["preflight"]["task_counts"]["SUCCESS"] == 501
    assert sum(result["preflight"]["task_counts"].values()) == 502


def test_preflight_reports_missing_source_sync_and_source_errors_separately(preflight_clock):
    _page(A)
    _page(B)
    with closing(db.get_connection()) as conn, conn:
        conn.execute("UPDATE page_rules SET source_synced_at=NULL WHERE page_id=?", (A,))
        conn.execute("UPDATE page_rules SET source_error='Synthetic Sheet configuration error' WHERE page_id=?", (B,))
    result = _snapshot()
    source_findings = {item["page_id"] for item in result["preflight"]["findings"]
                       if item.get("page_id") in (A, B)}
    assert source_findings == {A, B}
    assert result["preflight"]["source_errors"] == 2
    assert result["preflight"]["eligible_pages"] == 0


def test_preflight_orphan_operation_is_visible_without_recovery_writes(preflight_clock):
    _page(A)
    operation = safety.begin_operation("shared", page_id=A, kind="scan")
    assert operation["ready"]
    safety.abandon_operation("shared", operation["operation_id"])
    before = _dump()
    result = _snapshot()
    profile = _profiles(result)["shared"]
    assert profile["status"] == "BLOCKED"
    assert profile["reason"] == "PROFILE_BLOCKED"
    assert _dump() == before
    assert db.get_page(A)["browser_status"] == "READY"


def test_active_profile_operation_is_waiting_without_changing_dispatch_gate(preflight_clock):
    _page(A)
    operation = safety.begin_operation("shared", page_id=A, kind="check")
    assert operation["ready"]
    before = _dump()
    result = _snapshot()
    profile = _profiles(result)["shared"]
    assert profile["status"] == "WAITING" and profile["reason"] == "PROFILE_BUSY"
    assert result["preflight"]["eligible_pages"] == 0
    assert safety.status("shared")["operation_in_progress"] is True
    assert _dump() == before


def test_eligibility_is_preparation_independent_of_pause_and_disabled_auto(preflight_clock):
    _page(A, enabled=True)
    _page(B, enabled=False)
    paused = _snapshot(paused=True)["preflight"]
    running = _snapshot(paused=False)["preflight"]
    assert paused["eligible_pages"] == running["eligible_pages"] == 1
    assert paused["enabled_pages"] == running["enabled_pages"] == 1
    assert any(f["code"] == "SYSTEM_PAUSED" for f in paused["findings"])
    assert not any(f["code"] == "SYSTEM_PAUSED" for f in running["findings"])


def test_saved_source_proof_does_not_hide_missing_sheet_url(preflight_clock):
    _page(A)
    readiness = importlib.import_module("operational_readiness")
    result = readiness.snapshot(paused_all=True, sheet_sync={"sheet_url": ""})["preflight"]
    assert result["eligible_pages"] == 0 and result["source_errors"] == 1
    assert any(item["code"] == "SOURCE_INVALID" for item in result["findings"])


def test_api_readiness_failure_is_generic_and_does_not_change_pause(monkeypatch):
    import app
    from fastapi import HTTPException
    scheduler.SCHEDULER_METRICS["paused_all"] = True

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic-private-detail")

    monkeypatch.setattr(app.operational_readiness, "snapshot", broken)
    for endpoint in (app.preflight_overview, app.profile_safety_overview):
        with pytest.raises(HTTPException) as error:
            endpoint()
        assert error.value.status_code == 503
        assert "synthetic-private-detail" not in error.value.detail
    assert scheduler.SCHEDULER_METRICS["paused_all"] is True


@pytest.mark.parametrize("paused", [True, False])
def test_repeated_preflight_never_decrypts_fetches_or_changes_state(preflight_clock, monkeypatch, paused):
    _page(A)
    _page(GRAPH, mode="GRAPH_API")
    before = _dump()

    def forbidden(*args, **kwargs):
        raise AssertionError("Preflight must only inspect local public metadata")

    for module, name in (
        (crypto, "decrypt_token"), (db, "get_page_with_decrypted_token"),
        (db, "get_page_for_dispatch"), (scheduler, "refresh_comment_sheet"),
        (scheduler, "run_manual_sync_cycle"), (safety, "reserve_operation"),
        (safety, "begin_operation"), (safety, "clear_block"),
        (safety, "recover_interrupted_operations"),
        (meta_client, "verify_page_connection"),
        (antidetect_client, "verify_page_connection_cdp"),
    ):
        monkeypatch.setattr(module, name, forbidden)
    for _ in range(3):
        result = _snapshot(paused=paused)
        assert result["preflight"]["read_only"] is True
        assert result["preflight"]["paused_all"] is paused
        assert result["profile_safety"]["paused_all"] is paused
    assert _dump() == before
