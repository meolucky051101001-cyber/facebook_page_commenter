"""Shared runtime gates, entirely on temp DBs and synthetic sessions."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import threading
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
import pytest

import app
import browser_safety
import comment_dispatch
import config
import database as db
import meta_client
import operations
import scheduler
import submit_control
from test_dispatch import add_customer_comment
from test_auto_comment_fixes import queue_due


def _config_process(tmp_path, key=None, *, dotenv_disabled=True, script=None):
    # Copy source, never the operator's dotenv. Child environment is explicit.
    source = tmp_path / "config.py"
    source.write_text(Path(config.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    environment = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP") if name in os.environ}
    environment["PYTHON_DOTENV_DISABLED"] = "1" if dotenv_disabled else "0"
    environment["PYTHONIOENCODING"] = "utf-8"
    if key is not None:
        environment["ENCRYPTION_KEY"] = key
    code = script or "import runpy; runpy.run_path('config.py'); print('configured')"
    return subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=environment,
                          capture_output=True, text=True, encoding="utf-8", timeout=15)


@pytest.mark.parametrize("key", [None, "", "   "])
def test_missing_key_with_dotenv_disabled_fails_before_bootstrap(tmp_path, key):
    result = _config_process(tmp_path, key)
    assert result.returncode != 0
    assert "RuntimeError" in result.stderr
    assert not (tmp_path / ".env").exists()


def test_invalid_synthetic_key_has_static_error(tmp_path):
    invalid = "synthetic-invalid-key-do-not-echo"
    result = _config_process(tmp_path, invalid)
    assert result.returncode != 0
    assert invalid not in result.stdout + result.stderr
    assert not (tmp_path / ".env").exists()


def test_valid_synthetic_key_never_needs_dotenv(tmp_path):
    result = _config_process(tmp_path, Fernet.generate_key().decode())
    assert result.returncode == 0
    assert result.stdout.strip() == "configured"
    assert not (tmp_path / ".env").exists()


def test_bootstrap_key_is_durable_across_two_processes(tmp_path):
    (tmp_path / ".env.example").write_text("META_APP_ID=synthetic-app\n", encoding="utf-8")
    encrypt = """import runpy
from pathlib import Path
from cryptography.fernet import Fernet
settings = runpy.run_path('config.py')
Path('synthetic.bin').write_bytes(Fernet(settings['ENCRYPTION_KEY'].encode()).encrypt(b'synthetic payload'))
print('stored')
"""
    decrypt = """import runpy
from pathlib import Path
from cryptography.fernet import Fernet
settings = runpy.run_path('config.py')
assert Fernet(settings['ENCRYPTION_KEY'].encode()).decrypt(Path('synthetic.bin').read_bytes()) == b'synthetic payload'
print('recovered')
"""
    first = _config_process(tmp_path, dotenv_disabled=False, script=encrypt)
    second = _config_process(tmp_path, dotenv_disabled=False, script=decrypt)
    assert first.returncode == second.returncode == 0
    assert first.stdout.strip() == "stored"
    assert second.stdout.strip() == "recovered"


def test_startup_replaces_saved_running_with_persisted_pause(monkeypatch):
    db.set_setting("automation_run_state", "running")
    monkeypatch.setattr(config, "PAUSED_BY_DEFAULT", False)
    starts = []
    async def idle():
        starts.append(scheduler.SCHEDULER_METRICS["paused_all"])
    for module, names in ((scheduler, ("loop_post_scanner", "loop_task_executor", "loop_maintenance")),
                          (operations, ("loop_publications", "loop_inbox"))):
        for name in names:
            monkeypatch.setattr(module, name, idle)
    monkeypatch.setattr(app, "_drain_workers", lambda: None)
    async def startup():
        async with app.lifespan(app.app):
            await asyncio.sleep(0)
            assert scheduler.SCHEDULER_METRICS["paused_all"] is True
            assert db.get_setting("automation_run_state") == "paused"
    asyncio.run(startup())
    assert starts == [True] * 5


def test_startup_safety_recovery_failure_never_starts_workers(monkeypatch):
    def failed_recovery():
        raise RuntimeError("synthetic recovery failure")
    monkeypatch.setattr(browser_safety, "recover_interrupted_operations", failed_recovery)
    monkeypatch.setattr(asyncio, "create_task", lambda *args: pytest.fail("Worker started before safety recovery"))
    async def startup():
        async with app.lifespan(app.app):
            pytest.fail("Startup should fail closed")
    with pytest.raises(RuntimeError, match="synthetic recovery failure"):
        asyncio.run(startup())
    assert scheduler.SCHEDULER_METRICS["paused_all"] is True


@pytest.mark.parametrize("action", ["hide", "unhide"])
def test_pause_blocks_moderation_without_claim_or_post(ready_pages, monkeypatch, action):
    add_customer_comment(ready_pages[0])
    scheduler.SCHEDULER_METRICS["paused_all"] = True
    monkeypatch.setattr(meta_client, "page_request", lambda *args: pytest.fail("Paused moderation sent POST"))
    result = operations.moderate("12_35", action)
    assert result["reason"] == "SYSTEM_PAUSED"
    with closing(db.get_connection()) as conn:
        assert conn.execute("SELECT action_pending FROM incoming_comments").fetchone()[0] is None


@pytest.mark.parametrize("change", ["pause", "pause_resume", "limit"])
def test_late_moderation_policy_change_cancels_before_post(ready_pages, monkeypatch, change):
    page_id = ready_pages[0]
    add_customer_comment(page_id)
    original = db.get_page_for_dispatch
    reads = []
    def changed(*args, **kwargs):
        reads.append(1)
        if len(reads) == 2:
            if change == "limit":
                db.update_page_daily_limit(page_id, 5)
            else:
                scheduler.set_system_paused(True)
                if change == "pause_resume":
                    scheduler.set_system_paused(False)
        return original(*args, **kwargs)
    monkeypatch.setattr(db, "get_page_for_dispatch", changed)
    monkeypatch.setattr(meta_client, "page_request", lambda *args: pytest.fail("Stale moderation ticket sent POST"))
    result = operations.moderate("12_35", "hide")
    assert result["waiting"] is True
    with closing(db.get_connection()) as conn:
        assert conn.execute("SELECT action_pending FROM incoming_comments").fetchone()[0] is None


def test_moderation_restriction_quarantines_page_and_pending_tasks(ready_pages, monkeypatch):
    page_id = ready_pages[0]
    add_customer_comment(page_id)
    queue_due(page_id, page_id + "_101")
    def restricted(*args):
        raise meta_client.MetaAPIError(368, None, "Synthetic restriction", "RESTRICTED")
    monkeypatch.setattr(meta_client, "page_request", restricted)
    with pytest.raises(ValueError, match="Đối chiếu"):
        operations.moderate("12_35", "hide")
    assert db.get_page(page_id)["token_status"] == "RESTRICTED"
    assert {task["status"] for task in db.get_all_tasks(page_id=page_id)} == {"PAUSED"}


def test_pilot_requires_proof_in_ui_and_sql(ready_pages, monkeypatch):
    page_id = ready_pages[0]
    monkeypatch.setattr(config, "PILOT_PAGE_ID", page_id)
    db.upsert_page(page_id, "Synthetic Page", "synthetic-token", db.get_page(page_id)["account_id"])
    queue_due(page_id, page_id + "_101")
    page = db.get_page(page_id)
    assert app._public_graph_ready(page) is False
    assert db.get_due_tasks(page_id=page_id) == []
    task_id = db.get_all_tasks(page_id=page_id)[0]["id"]
    assert db.claim_due_task(task_id, db.datetime.now(db.timezone.utc).isoformat()) is None


def test_unchanged_sheet_rule_does_not_cancel_ticket(ready_pages):
    page_id = ready_pages[0]
    page = db.get_page(page_id)
    ticket = submit_control.snapshot(page_id, automatic=True)
    db.update_page_rule(page_id, page["delay_minutes"], json.loads(page["templates"]),
                        page["max_comments_per_day"], bool(page["rule_enabled"]))
    assert submit_control.commit(ticket, lambda: False) is True


def test_automation_mutation_invalidates_auto_only(ready_pages):
    page_id = ready_pages[0]
    automatic = submit_control.snapshot(page_id, automatic=True)
    manual = submit_control.snapshot(page_id)
    db.set_page_automation(page_id, False)
    db.set_page_automation(page_id, True)
    assert submit_control.commit(automatic, lambda: False) is False
    assert submit_control.commit(manual, lambda: False) is True


def test_pending_database_mutation_prevents_admission(ready_pages, monkeypatch):
    page_id = ready_pages[0]
    entered, release = threading.Event(), threading.Event()
    original = db.get_connection
    def blocked_connection():
        entered.set()
        assert release.wait(5)
        return original()
    monkeypatch.setattr(db, "get_connection", blocked_connection)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(db.update_page_daily_limit, page_id, 7)
        try:
            assert entered.wait(5)
            ticket = submit_control.snapshot(page_id)
            assert submit_control.commit(ticket, lambda: False) is False
        finally:
            release.set()
        pending.result(timeout=5)
    assert submit_control.commit(ticket, lambda: False) is False


@pytest.mark.parametrize("action", ["hide", "reply"])
def test_failed_restriction_save_keeps_operation_marker_for_restart(ready_pages, monkeypatch, action):
    page_id = ready_pages[0]
    add_customer_comment(page_id)
    queue_due(page_id, page_id + "_101")
    def restricted(*args):
        raise meta_client.MetaAPIError(368, None, "Synthetic restriction", "RESTRICTED")
    def failed_status(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic status persistence failure")
    monkeypatch.setattr(meta_client, "page_request", restricted)
    monkeypatch.setattr(db, "update_page_status", failed_status)
    with pytest.raises(sqlite3.OperationalError):
        operations.moderate("12_35", action, "Synthetic reply")
    assert comment_dispatch.graph_quarantined(page_id)
    assert not app._public_graph_ready(db.get_page(page_id))
    with closing(db.get_connection()) as conn:
        assert conn.execute("SELECT action_pending FROM incoming_comments").fetchone()[0] == action
    # Simulate loss of process-local state; the original claim is durable.
    comment_dispatch.clear_graph_quarantine(page_id)
    with closing(db.get_connection()) as conn:
        operations.recover_interrupted_operations(conn)
    assert db.get_page(page_id)["connection_checked_at"] is None
    assert db.get_page(page_id)["token_status"] == "UNVERIFIED"
    assert {task["status"] for task in db.get_all_tasks(page_id=page_id)} == {"PAUSED"}


def test_graph_sending_recovery_requires_fresh_proof_and_keeps_history(ready_pages):
    page_id = ready_pages[0]
    queue_due(page_id, page_id + "_101")
    queue_due(page_id, page_id + "_102")
    task_id = db.get_all_tasks(page_id=page_id)[0]["id"]
    assert db.claim_due_task(task_id, datetime.now(timezone.utc).isoformat())
    assert db.recover_stale_tasks(startup=True) == 1
    assert {task["status"] for task in db.get_all_tasks(page_id=page_id)} == {"NEED_CHECK", "PAUSED"}
    page = db.get_page(page_id)
    assert page["comments_today_count"] == 0
    assert page["connection_checked_at"] is None
    assert page["token_status"] == "UNVERIFIED"
    db.update_page_connection_check(page_id, datetime.now(timezone.utc).isoformat(), token_status="OK")
    assert {task["status"] for task in db.get_all_tasks(page_id=page_id)} == {"NEED_CHECK", "PAUSED"}


def test_failed_publication_restriction_save_recovers_without_retry(ready_pages, monkeypatch):
    page_id = ready_pages[0]
    entry = app.PublicationRequest(page_id=page_id, external_key="synthetic-publication",
                                   caption="Synthetic post", scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    assert operations.add_publications([entry]) == 1
    def restricted(*args):
        raise meta_client.MetaAPIError(368, None, "Synthetic restriction", "RESTRICTED")
    def failed_status(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic status persistence failure")
    monkeypatch.setattr(meta_client, "publish_page_post", restricted)
    monkeypatch.setattr(db, "update_page_status", failed_status)
    with pytest.raises(sqlite3.OperationalError):
        operations.process_publications()
    assert operations.list_publications()[0]["status"] == "PUBLISHING"
    assert comment_dispatch.graph_quarantined(page_id)
    comment_dispatch.clear_graph_quarantine(page_id)
    with closing(db.get_connection()) as conn:
        operations.recover_interrupted_operations(conn)
    assert operations.list_publications()[0]["status"] == "NEED_CHECK"
    assert db.get_page(page_id)["connection_checked_at"] is None


def test_old_graph_proof_cannot_clear_quarantine_after_reconnect(ready_pages):
    page_id = ready_pages[0]
    original = db.get_page_for_dispatch(page_id)
    comment_dispatch.quarantine_graph(page_id)
    db.upsert_page(page_id, "Reconnected synthetic Page", "different-synthetic-token", original["account_id"])
    assert db.update_page_connection_check(page_id, datetime.now(timezone.utc).isoformat(),
                                            token_status="OK", expected_connection=original) is False
    assert db.get_page(page_id)["connection_checked_at"] is None
    assert comment_dispatch.graph_quarantined(page_id)
    current = db.get_page_for_dispatch(page_id)
    assert db.update_page_connection_check(page_id, datetime.now(timezone.utc).isoformat(),
                                            token_status="OK", expected_connection=current) is True
    assert not comment_dispatch.graph_quarantined(page_id)


def test_pause_during_resume_preparation_cannot_be_overridden(ready_pages, monkeypatch):
    scheduler.set_system_paused(True)
    writes = []
    original = db.set_setting
    def record(key, value):
        writes.append((key, value))
        return original(key, value)
    def refresh(**kwargs):
        assert app.set_pause(app.PauseRequest(paused=True))["paused"] is True
        return {"success": True}
    monkeypatch.setattr(db, "set_setting", record)
    monkeypatch.setattr(scheduler, "refresh_comment_sheet", refresh)
    result = app.set_pause(app.PauseRequest(paused=False))
    assert result == {"success": True, "paused": True}
    assert scheduler.SCHEDULER_METRICS["paused_all"] is True
    assert db.get_setting("automation_run_state") == "paused"
    assert writes == [("automation_run_state", "paused")]
