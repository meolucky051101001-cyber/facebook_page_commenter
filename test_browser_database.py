from contextlib import closing
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

import comment_dispatch
import crypto
import database as db
import migrations
from test_migrations_runtime import v5_database


@pytest.fixture
def browser_page():
    pid = "615000000000001"
    account = db.upsert_account("Browser account")
    db.upsert_antidetect_page(pid, "Browser Page", f"https://www.facebook.com/{pid}",
                              "profile-one", "Profile 1", account)
    db.set_page_send_mode(pid, "BROWSER")
    assert db.update_page_browser_check(pid, browser_status="READY", identity_id=pid, profile_id="profile-one")
    return pid


def due_task(pid, post="1"):
    when = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert db.create_task(pid, f"{pid}_{post}", "Saved content", when, source="MANUAL")
    return next(task for task in db.get_all_tasks(page_id=pid) if task["post_id"] == f"{pid}_{post}")


def test_v7_migration_preserves_history_consent_and_graph_defaults(tmp_path):
    with closing(v5_database(tmp_path / "old.sqlite3")) as conn:
        conn.execute("UPDATE pages SET comments_today_count=5,last_comment_at='sent-at'")
        conn.commit()
        before = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        migrations.run_migrations(conn)
        migrations.run_migrations(conn)
        after = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        assert [row[:len(before[0])] for row in after] == before
        assert conn.execute("SELECT send_mode,browser_status,comments_today_count,last_comment_at FROM pages").fetchone() == (
            "GRAPH_API", "UNCHECKED", 5, "sent-at")
        assert conn.execute("SELECT is_enabled FROM page_rules").fetchone() == (1,)
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (7,)
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_v7_migration_rolls_back_partial_columns(tmp_path):
    with closing(v5_database(tmp_path / "old.sqlite3")) as conn:
        class Interrupted:
            def __getattr__(self, name):
                return getattr(conn, name)

            def __enter__(self):
                return conn.__enter__()

            def __exit__(self, *args):
                return conn.__exit__(*args)

            def execute(self, sql, *args):
                if "ADD COLUMN browser_status" in sql:
                    raise sqlite3.OperationalError("interrupted v7")
                return conn.execute(sql, *args)

        with pytest.raises(sqlite3.OperationalError, match="interrupted v7"):
            migrations.run_migrations(Interrupted())
        assert "send_mode" not in [row[1] for row in conn.execute("PRAGMA table_info(pages)")]
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (6,)
        migrations.run_migrations(conn)
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (7,)


@pytest.mark.parametrize("stored_token", ["", "broken-token"])
def test_browser_dispatch_does_not_decrypt_or_require_graph(browser_page, monkeypatch, stored_token):
    with db.get_connection() as conn:
        conn.execute("UPDATE pages SET page_access_token_enc=? WHERE page_id=?", (stored_token, browser_page))

    def no_decryption(*args):
        raise AssertionError("Browser dispatch must not decrypt Graph tokens")

    monkeypatch.setattr(crypto, "decrypt_token", no_decryption)
    page = db.get_page_for_dispatch(browser_page)
    assert comment_dispatch.send_ready(page)
    assert comment_dispatch.check(browser_page, lambda: False, transport="selected").ready
    assert db.get_page(browser_page)["token_status"] == "ANTIDETECT_READY"
    assert "page_access_token_enc" not in page


def test_browser_candidate_claim_release_uses_verified_route(browser_page):
    task = due_task(browser_page)
    assert [row["id"] for row in db.get_due_tasks()] == [task["id"]]
    assert db.claim_due_task(task["id"], datetime.now(timezone.utc).isoformat())
    assert db.release_task_lock(task["id"])
    assert db.get_all_tasks()[0]["status"] == "PENDING"
    assert db.claim_due_task(task["id"], datetime.now(timezone.utc).isoformat())
    assert db.invalidate_page_browser(browser_page, "LOGIN_REQUIRED", "Đăng nhập lại.", profile_id="profile-one", identity_id=browser_page)
    assert db.release_task_lock(task["id"])
    assert db.get_all_tasks()[0]["status"] == "PAUSED"
    assert not db.get_due_tasks()
    assert db.get_page(browser_page)["comments_today_count"] == 0


def test_mode_change_pauses_pending_without_resetting_consent_or_history(browser_page):
    task = due_task(browser_page)
    db.set_page_automation(browser_page, True)
    before = db.get_page(browser_page)
    with db.get_connection() as conn:
        db.record_comment_success(conn, browser_page, datetime.now(timezone.utc))
    result = db.set_page_send_mode(browser_page, "GRAPH_API")
    assert result["send_mode"] == "GRAPH_API" and result["browser_status"] == "UNCHECKED"
    assert result["browser_identity_id"] is None
    assert result["rule_enabled"] == before["rule_enabled"] == 1
    assert result["last_seen_post_time"] == before["last_seen_post_time"]
    assert result["comments_today_count"] == 1
    assert "page_access_token_enc" not in result and "page_access_token" not in result
    assert db.get_all_tasks()[0]["id"] == task["id"]
    assert db.get_all_tasks()[0]["status"] == "PAUSED"
    db.set_page_send_mode(browser_page, "BROWSER")
    assert db.update_page_browser_check(browser_page, browser_status="READY", identity_id=browser_page, profile_id="profile-one")
    assert db.get_all_tasks()[0]["status"] == "PAUSED"


def test_mode_and_profile_changes_reject_inflight_send(browser_page):
    task = due_task(browser_page)
    assert db.claim_due_task(task["id"], datetime.now(timezone.utc).isoformat())
    with pytest.raises(ValueError, match="đang gửi"):
        db.set_page_send_mode(browser_page, "GRAPH_API")
    result = db.upsert_antidetect_page(browser_page, "Browser Page", None, "profile-two", "Profile 2",
                                      db.get_page(browser_page)["account_id"])
    assert result["action"] == "conflict"
    page = db.get_page(browser_page)
    assert page["send_mode"] == "BROWSER" and page["antidetect_profile_id"] == "profile-one"
    assert page["browser_status"] == "READY"


def test_stale_browser_proof_cannot_follow_profile_or_mode_change(browser_page):
    due_task(browser_page)
    db.upsert_antidetect_page(browser_page, "Browser Page", None, "profile-two", "Profile 2",
                              db.get_page(browser_page)["account_id"])
    assert not db.update_page_browser_check(browser_page, browser_status="READY", identity_id=browser_page, profile_id="profile-one")
    assert db.get_page(browser_page)["browser_status"] == "UNCHECKED"
    assert not comment_dispatch.send_ready(db.get_page_for_dispatch(browser_page))
    assert not db.get_due_tasks()
    assert db.update_page_browser_check(browser_page, browser_status="READY", identity_id=browser_page, profile_id="profile-two")
    assert not db.invalidate_page_browser(browser_page, "ERROR", "Old failure", profile_id="profile-one", identity_id=browser_page)
    assert db.get_page(browser_page)["browser_status"] == "READY"
    db.set_page_send_mode(browser_page, "GRAPH_API")
    assert not db.update_page_browser_check(browser_page, browser_status="READY", identity_id=browser_page, profile_id="profile-two")


def test_identity_mismatch_blocks_sql_and_policy_even_with_ready_status(browser_page):
    task = due_task(browser_page)
    with db.get_connection() as conn:
        conn.execute("UPDATE pages SET fb_profile_id='615000000000099' WHERE page_id=?", (browser_page,))
    assert not comment_dispatch.send_ready(db.get_page_for_dispatch(browser_page))
    assert not db.get_due_tasks()
    assert not db.claim_due_task(task["id"], datetime.now(timezone.utc).isoformat())


def test_graph_failure_does_not_disable_selected_browser(browser_page):
    task = due_task(browser_page)
    with db.get_connection() as conn:
        conn.execute("UPDATE pages SET page_access_token_enc='broken-token' WHERE page_id=?", (browser_page,))
    assert not comment_dispatch.check(browser_page, lambda: False).ready
    db.update_page_connection_check(browser_page, datetime.now(timezone.utc).isoformat(), "Expired", "EXPIRED")
    db.update_page_status(browser_page, False, "EXPIRED", "Expired")
    assert comment_dispatch.check(browser_page, lambda: False, transport="selected").ready
    assert db.get_all_tasks()[0]["status"] == "PENDING"
    assert [row["id"] for row in db.get_due_tasks()] == [task["id"]]


def test_browser_shares_daily_counter_gap_and_graph_only_operations(browser_page):
    assert comment_dispatch.check(browser_page, lambda: False).reason == "PAGE_BLOCKED"
    db.update_page_daily_limit(browser_page, 1)
    with db.get_connection() as conn:
        db.record_comment_success(conn, browser_page, datetime.now(timezone.utc))
    assert comment_dispatch.check(browser_page, lambda: False, transport="selected").reason == "WAIT_DAILY"
    assert comment_dispatch.page_status(db.get_page(browser_page))["comments_remaining_today"] == 0


def test_browser_scan_cannot_commit_after_mode_change(browser_page):
    db.set_page_automation(browser_page, True)
    snapshot = db.get_page_for_dispatch(browser_page)
    db.set_page_send_mode(browser_page, "GRAPH_API")
    assert not db.queue_auto_scan(snapshot, [], datetime.now(timezone.utc).isoformat())
    assert db.get_page(browser_page)["last_seen_post_time"] == snapshot["last_seen_post_time"]
