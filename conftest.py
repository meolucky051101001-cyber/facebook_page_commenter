"""Isolated test runtime: never use the operator's database or HTTP credentials."""
import copy
import os
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
import httpx
import pytest

os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
for _key in ("META_APP_ID", "META_APP_SECRET", "META_OAUTH_REDIRECT_URI",
             "MULTI_PAGE_SHEET_URL", "PILOT_SHEET_URL", "DEFAULT_SHEET_URL", "PILOT_PAGE_ID"):
    os.environ[_key] = ""
os.environ["DASHBOARD_ADMIN_KEY"] = "isolated-test-dashboard-key-0000000000"

import config
import crypto
import database as db
import scheduler


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "commenter.sqlite3")
    monkeypatch.setattr(crypto, "_fernet", None)
    monkeypatch.setattr(config, "ANTIDETECT_DIR", str(tmp_path / "no-browser-profiles"))
    monkeypatch.setattr(scheduler, "SCHEDULER_METRICS", copy.deepcopy(scheduler.SCHEDULER_METRICS))
    scheduler.SCHEDULER_METRICS["paused_all"] = False
    monkeypatch.setattr(scheduler, "_comment_sheet_result", {"success": False})
    monkeypatch.setattr(scheduler, "_last_comment_sheet_read", float("-inf"))
    monkeypatch.setattr(scheduler, "_SCAN_BACKOFF_UNTIL", {})

    def no_http(*args, **kwargs):
        raise AssertionError("Tests must mock HTTP; real network access is disabled")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_http)
    db.init_db()


@pytest.fixture
def clock(monkeypatch):
    import sys

    class Clock(datetime):
        value = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)

        @classmethod
        def advance(cls, **kwargs):
            cls.value += timedelta(**kwargs)

    for name in ("database", "scheduler", "operations", "comment_dispatch", "meta_client"):
        module = sys.modules.get(name)
        if module and hasattr(module, "datetime"):
            monkeypatch.setattr(module, "datetime", Clock)
    return Clock


@pytest.fixture
def ready_pages(monkeypatch):
    import meta_client
    import sheets_sync

    ids = ("100000000000001", "100000000000002")
    for pid in ids:
        db.upsert_page(pid, "Page " + pid[-1], "mock-" + pid, db.upsert_account(pid))
        db.update_page_connection_check(pid, datetime.now(timezone.utc).isoformat(), token_status="OK")
        db.set_page_automation(pid, True)
    sheets_sync.sync_comment_sheet_rows([
        ["ID trang", "Tên Fanpage", "Comment", "Số phút chờ sau khi đăng"],
        [ids[0], "A", "Template A", "10"], [ids[1], "B", "Template B", "10"],
    ])
    monkeypatch.setattr(scheduler, "comment_sheet_url", lambda: "mock-sheet")
    monkeypatch.setattr(scheduler, "refresh_comment_sheet", lambda **kwargs: {"success": True})
    monkeypatch.setattr(scheduler, "_comment_sheet_result", {"success": True})
    monkeypatch.setattr(meta_client, "verify_post_belongs_to_page",
                        lambda post, *args: {"clean_post_id": post})
    return ids
