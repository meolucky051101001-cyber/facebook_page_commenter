from contextlib import closing
import threading

import pytest

import comment_dispatch
import database as db
import scheduler
import submit_control
from test_auto_comment_fixes import queue_due


def test_pause_resume_does_not_revive_old_ticket():
    old = submit_control.snapshot("page-a")
    submit_control.invalidate_all()
    submit_control.invalidate_all()
    assert not submit_control.commit(old, lambda: False)
    current = submit_control.snapshot("page-a")
    assert submit_control.commit(current, lambda: False)
    assert not submit_control.commit(current, lambda: False)


def test_page_automation_change_leaves_manual_and_other_page_tickets_independent():
    auto = submit_control.snapshot("page-a", automatic=True)
    manual = submit_control.snapshot("page-a")
    other = submit_control.snapshot("page-b", automatic=True)
    submit_control.invalidate_automation("page-a")
    assert not submit_control.commit(auto, lambda: False)
    assert submit_control.commit(manual, lambda: False)
    assert submit_control.commit(other, lambda: False)


def test_profile_block_invalidates_siblings_without_cancelling_graph_or_other_profiles():
    first = submit_control.snapshot("page-a", profile_id="profile-shared")
    sibling = submit_control.snapshot("page-b", profile_id="profile-shared")
    other = submit_control.snapshot("page-c", profile_id="profile-other")
    graph = submit_control.snapshot("page-a")
    submit_control.invalidate_profile("profile-shared")
    assert not submit_control.commit(first, lambda: False)
    assert not submit_control.commit(sibling, lambda: False)
    assert submit_control.commit(other, lambda: False)
    assert submit_control.commit(graph, lambda: False)


def test_profile_stop_publication_rejects_tickets_created_during_the_mutation():
    with submit_control.profile_mutation("profile-shared"):
        during = submit_control.snapshot("page-a", profile_id="profile-shared")
        assert not submit_control.commit(during, lambda: False)
        assert submit_control.commit(submit_control.snapshot("page-b", profile_id="profile-other"), lambda: False)
    assert not submit_control.commit(during, lambda: False)


def test_mutation_rejects_new_tickets_until_commit_and_invalidates_them_afterward():
    with submit_control.mutation("page-a"):
        during = submit_control.snapshot("page-a")
        assert not submit_control.commit(during, lambda: False)
        assert submit_control.commit(submit_control.snapshot("page-b"), lambda: False)
    assert not submit_control.commit(during, lambda: False)
    assert submit_control.commit(submit_control.snapshot("page-a"), lambda: False)


def test_failed_pause_persistence_still_cancels_submission(monkeypatch):
    ticket = submit_control.snapshot("page-a")
    def fail(*args):
        raise RuntimeError("synthetic DB failure")
    monkeypatch.setattr(db, "set_setting", fail)
    with pytest.raises(RuntimeError):
        scheduler.set_system_paused(True)
    assert scheduler.SCHEDULER_METRICS["paused_all"] is True
    assert not submit_control.commit(ticket, lambda: False)


def test_pause_publishes_before_waiting_for_an_older_resume(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    paused_published = threading.Event()
    class Metrics(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            if key == "paused_all" and value is True:
                paused_published.set()
    monkeypatch.setattr(scheduler, "SCHEDULER_METRICS", Metrics(scheduler.SCHEDULER_METRICS))
    def write(key, value):
        if value == "running":
            entered.set()
            assert release.wait(3)
    monkeypatch.setattr(db, "set_setting", write)
    resume = threading.Thread(target=scheduler.set_system_paused, args=(False,))
    pause = threading.Thread(target=scheduler.set_system_paused, args=(True,))
    resume.start()
    assert entered.wait(3)
    pause.start()
    try:
        # The pause publishes before it waits for the persistence mutex.
        assert paused_published.wait(3)
        assert not submit_control.commit(submit_control.snapshot("page-a"), lambda: False)
    finally:
        release.set()
        resume.join(3)
        pause.join(3)
    assert not resume.is_alive() and not pause.is_alive()
    assert scheduler.SCHEDULER_METRICS["paused_all"] is True


def test_stale_resume_request_skips_persistence(monkeypatch):
    version = scheduler.begin_run_state_request(False)
    scheduler.set_system_paused(True)
    monkeypatch.setattr(db, "set_setting", lambda *args: pytest.fail("Stale Resume wrote run state"))
    assert scheduler.set_system_paused(False, expected_version=version) is True
    assert scheduler.SCHEDULER_METRICS["paused_all"] is True


def test_pause_request_publication_rejects_intermediate_admission(monkeypatch):
    class PublishingMetrics(dict):
        def __setitem__(self, key, value):
            if key == "paused_all" and value is True:
                # This runs immediately before the published pause flag changes.
                during = submit_control.snapshot("page-a")
                assert not submit_control.commit(during, lambda: False)
            super().__setitem__(key, value)
    monkeypatch.setattr(scheduler, "SCHEDULER_METRICS", PublishingMetrics(scheduler.SCHEDULER_METRICS))
    scheduler.begin_run_state_request(True)
    assert scheduler.SCHEDULER_METRICS["paused_all"] is True


def test_final_gate_does_not_wait_for_sheet_refresh(ready_pages):
    page_id = ready_pages[0]
    queue_due(page_id, page_id + "_1")
    task = db.get_all_tasks(page_id=page_id)[0]
    ticket = submit_control.snapshot(page_id)
    page = db.get_page_for_dispatch(page_id)
    with scheduler._COMMENT_SHEET_LOCK:
        decision = scheduler._final_submit_gate(task, page, lambda: False, ticket)
    assert decision.reason == "SOURCE_BUSY"
    assert ticket.committed is False


def test_final_policy_rechecks_claim_and_current_daily_limit(ready_pages):
    page_id = ready_pages[0]
    queue_due(page_id, page_id + "_1")
    task = db.get_all_tasks(page_id=page_id)[0]
    page = db.get_page_for_dispatch(page_id)
    task = db.claim_due_task(task["id"], db.datetime.now(db.timezone.utc).isoformat())
    assert comment_dispatch.final_check(page_id, lambda: False, page, task=task).ready
    with closing(db.get_connection()) as conn, conn:
        conn.execute("UPDATE pages SET comments_today_count=20,comments_today_date=? WHERE page_id=?",
                     (db.local_day_key(db.datetime.now(db.timezone.utc)), page_id))
    assert comment_dispatch.final_check(page_id, lambda: False, page, task=task).reason == "WAIT_DAILY"
    with closing(db.get_connection()) as conn, conn:
        conn.execute("UPDATE tasks SET comment_content='changed' WHERE id=?", (task["id"],))
    assert comment_dispatch.final_check(page_id, lambda: False, page, task=task).reason == "TASK_CHANGED"


def test_final_policy_database_lock_defers_without_long_wait(ready_pages):
    page_id = ready_pages[0]
    page = db.get_page_for_dispatch(page_id)
    with closing(db.get_connection()) as conn:
        conn.execute("BEGIN EXCLUSIVE")
        decision = comment_dispatch.final_check(page_id, lambda: False, page)
        conn.rollback()
    assert decision.reason == "POLICY_UNAVAILABLE"
