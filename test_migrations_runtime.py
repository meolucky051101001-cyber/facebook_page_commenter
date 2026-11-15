import asyncio
from contextlib import closing
import json
import sqlite3
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

import app
import config
import database as db
import meta_client
import migrations
import scheduler
import service_lock
from test_dispatch import add_customer_comment


def v5_database(path):
    """Build the last shipped schema from explicit old-version definitions."""
    conn = sqlite3.connect(path)
    migrations._apply_migration_v1(conn)
    migrations._apply_migration_v2(conn)
    migrations._apply_migration_v3(conn)
    conn.execute("ALTER TABLE page_rules ADD COLUMN comment_sequence TEXT NOT NULL DEFAULT '[]'")
    conn.execute("ALTER TABLE tasks ADD COLUMN sequence_step INTEGER")
    conn.execute("ALTER TABLE tasks ADD COLUMN sequence_gap_minutes INTEGER")
    conn.execute("CREATE UNIQUE INDEX idx_tasks_sequence_step ON tasks(page_id,post_id,sequence_step) WHERE source='AUTO' AND sequence_step IS NOT NULL")
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_version VALUES (5)")
    conn.execute("INSERT INTO pages(page_id,page_name,page_access_token_enc,last_seen_post_time) VALUES ('page','Page','encoded','cursor')")
    conn.execute("INSERT INTO page_rules(page_id,is_enabled,comment_sequence) VALUES ('page',1,?)",
                 (json.dumps([{"template":"One", "delay_minutes":10}]),))
    for i, status in enumerate(("PENDING","PAUSED","SENDING","SUCCESS","FAILED","NEED_CHECK","CANCELLED")):
        conn.execute("""INSERT INTO tasks(page_id,post_id,dedupe_key,comment_content,scheduled_at,status,sequence_step,sequence_gap_minutes)
            VALUES ('page',?,?, 'Saved text','2026-11-15T10:00:00+00:00',?,1,0)""", (str(i),str(i),status))
    conn.commit()
    return conn


def test_v6_migration_preserves_all_statuses_and_is_repeatable(tmp_path):
    with closing(v5_database(tmp_path / "old.sqlite3")) as conn:
        before = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        migrations.run_migrations(conn)
        migrations.run_migrations(conn)
        after = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        assert [row[:len(before[0])] for row in after] == before
        assert conn.execute("SELECT last_seen_post_time FROM pages").fetchone() == ("cursor",)
        assert conn.execute("SELECT is_enabled FROM page_rules").fetchone() == (1,)
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (6,)
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_v6_migration_rolls_back_if_interrupted(tmp_path):
    with closing(v5_database(tmp_path / "old.sqlite3")) as conn:
        class Interrupted:
            def __getattr__(self, name):
                return getattr(conn, name)

            def __enter__(self):
                return conn.__enter__()

            def __exit__(self, *args):
                return conn.__exit__(*args)

            def execute(self, sql, *args):
                if "ALTER TABLE tasks ADD COLUMN template_source" in sql:
                    raise sqlite3.OperationalError("simulated interrupted migration")
                return conn.execute(sql, *args)

        with pytest.raises(sqlite3.OperationalError):
            migrations.run_migrations(Interrupted())
        assert "send_cooldown_until" not in [row[1] for row in conn.execute("PRAGMA table_info(pages)")]
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (5,)
        migrations.run_migrations(conn)
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (6,)


def test_lifespan_rejects_second_owner_before_recovery(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "init_db", lambda: calls.append("init"))
    with service_lock.exclusive_database(db.DB_PATH):
        with pytest.raises(RuntimeError, match="Commenter"):
            with TestClient(app.app, base_url="http://localhost"):
                pass
    assert calls == []


def test_cancelling_loop_waits_for_actual_worker_to_finish():
    release = threading.Event()
    finished = threading.Event()

    async def scenario():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def worker():
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5), "Test did not release the background worker"
            finished.set()

        pending = asyncio.create_task(scheduler.run_worker(worker))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            pending.cancel()
            await asyncio.sleep(0)  # Yield one scheduler turn so cancellation is delivered.
            assert not pending.done(), "Service shutdown must wait for the actual worker"
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert finished.is_set()

    asyncio.run(scenario())


def test_dashboard_auth_and_structured_wait_response(ready_pages, clock):
    a, _ = ready_pages
    add_customer_comment(a)
    with db.get_connection() as conn:
        db.record_comment_success(conn, a, clock.now(clock.value.tzinfo))
    client = TestClient(app.app, base_url="http://localhost")
    assert client.get("/api/pages").status_code == 401
    client.headers["X-Dashboard-Key"] = config.DASHBOARD_ADMIN_KEY
    response = client.post("/api/inbox/12_35/moderate", json={"action":"reply","message":"Draft"})
    assert response.status_code == 429
    assert response.json()["reason"] == "WAIT_GAP"
    assert response.json()["retry_at"]
    page = next(page for page in client.get("/api/pages").json()["pages"] if page["page_id"] == a)
    assert page["comments_used_today"] == 1 and page["comments_remaining_today"] == 19
    assert page["next_send_at"]
    client.close()


def test_meta_rate_limit_retains_scope_and_retry_after(monkeypatch):
    response = httpx.Response(429, json={"error":{"code":4,"message":"rate limit"}}, headers={"Retry-After":"600"})
    monkeypatch.setattr(httpx.Client, "post", lambda *args, **kwargs: response)
    result = meta_client.post_comment("1_2","1","mock","Text")
    assert result["category"] == "RATE_LIMIT"
    assert result["rate_limit_scope"] == "APP" and result["retry_after_seconds"] == 600
