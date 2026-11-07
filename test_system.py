import os
import json
import sqlite3
from datetime import datetime, timezone, timedelta
import httpx
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

import config
import crypto
import database as db
import spintax
import scheduler
import meta_client
import sheets_sync
import app as fastapi_app


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    # Isolated test database for every test
    test_db_path = tmp_path / "test_commenter.db"
    db.DB_PATH = test_db_path
    db.init_db()
    yield


def test_spintax_parsing():
    text = "{Chào|Xin chào} {bạn|anh chị}, shop {rất vui|hân hạnh} được phục vụ!"
    results = {spintax.spin(text) for _ in range(30)}
    assert len(results) > 1
    for r in results:
        assert "{" not in r and "}" not in r


def test_crypto_fernet_and_masking():
    raw_token = "EAABwz1234567890abcdefXYZ"
    encrypted = crypto.encrypt_token(raw_token)
    assert encrypted != raw_token
    assert len(encrypted) > 20

    decrypted = crypto.decrypt_token(encrypted)
    assert decrypted == raw_token

    masked = crypto.mask_token(raw_token)
    assert masked == "...defXYZ"
    assert "123456" not in masked


def test_database_token_safety():
    acc_id = db.upsert_account("Nick Test", "10001", "raw_user_token_abc")
    db.upsert_page("page_sec_1", "Page Bảo Mật", "EAAB_super_secret_token", acc_id)

    # API function get_all_pages MUST NOT leak raw token!
    pages = db.get_all_pages()
    assert len(pages) == 1
    assert "page_access_token" not in pages[0]
    assert "page_access_token_enc" not in pages[0]

    # Internal worker function gets decrypted token correctly
    worker_page = db.get_page_with_decrypted_token("page_sec_1")
    assert worker_page["page_access_token"] == "EAAB_super_secret_token"


def test_deduplication_auto_vs_manual():
    acc_id = db.upsert_account("Nick Test 2")
    db.upsert_page("page_dedupe", "Page Dedupe", "token_123", acc_id)
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. AUTO task creation
    created1 = db.create_task("page_dedupe", "post_500", "Comment auto 1", now_iso, source="AUTO")
    assert created1 is True

    # 2. AUTO duplicate should be rejected
    created2 = db.create_task("page_dedupe", "post_500", "Comment auto 2", now_iso, source="AUTO")
    assert created2 is False

    # 3. MANUAL task with batch_1 should be allowed even on same post
    created_manual1 = db.create_task("page_dedupe", "post_500", "Comment manual 1", now_iso, source="MANUAL", batch_id="b1")
    assert created_manual1 is True

    # 4. Same MANUAL batch duplicate should be rejected
    created_manual2 = db.create_task("page_dedupe", "post_500", "Comment manual 1", now_iso, source="MANUAL", batch_id="b1")
    assert created_manual2 is False


def test_task_execution_lifecycle():
    acc_id = db.upsert_account("Nick 2")
    db.upsert_page("page_888", "Shop Quần Áo", "token_888", acc_id)

    past_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    db.create_task("page_888", "post_2002", "Test content", past_time)

    with patch("meta_client.post_comment") as mock_post:
        mock_post.return_value = {"success": True, "comment_id": "comment_fb_7777", "usage_pct": 12.5}
        scheduler.execute_due_tasks()

    tasks = db.get_all_tasks(page_id="page_888")
    assert len(tasks) == 1
    assert tasks[0]["status"] == "SUCCESS"
    assert tasks[0]["comment_id"] == "comment_fb_7777"

    page = db.get_page("page_888")
    assert page["comments_today_count"] == 1
    assert page["api_usage_percent"] == 12.5


def test_task_cancellation():
    acc_id = db.upsert_account("Nick 3")
    db.upsert_page("page_777", "Page Test", "token_777", acc_id)

    future_time = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    db.create_task("page_777", "post_3003", "Pending task", future_time)

    tasks = db.get_all_tasks(page_id="page_777")
    task_id = tasks[0]["id"]
    assert tasks[0]["status"] == "PENDING"

    cancelled = db.cancel_task(task_id)
    assert cancelled is True

    updated_tasks = db.get_all_tasks(page_id="page_777")
    assert updated_tasks[0]["status"] == "CANCELLED"


def test_google_sheet_sync_safe_no_token():
    acc_id = db.upsert_account("Nick Minh", "10002")
    db.upsert_page("102938475612345", "Shop Thời Trang", "existing_token_xyz", acc_id)

    mock_pages = [
        {"page id": "'102938475612345", "tên gợi nhớ": "Shop Thời Trang", "delay (phút)": "25", "giới hạn comment/ngày": "15", "bật tự động": "TRUE"}
    ]
    mock_comments = [
        {"page id": "'102938475612345", "mẫu bình luận": "{Dạ chào|Hello} bạn nhé!", "bật": "TRUE"}
    ]

    with patch("sheets_sync.fetch_sheet_csv") as mock_fetch:
        def fetch_side_effect(sheet_id, tab):
            if tab == "Pages":
                return mock_pages, None
            elif tab == "Comments":
                return mock_comments, None
            return [], None
        mock_fetch.side_effect = fetch_side_effect

        res = sheets_sync.sync_from_google_sheet("https://docs.google.com/spreadsheets/d/mock123/edit")
        assert res["success"] is True
        assert res["pages_synced"] == 1
        assert res["errors"] == []

    p = db.get_page("102938475612345")
    assert p["delay_minutes"] == 25
    assert p["max_comments_per_day"] == 15
    assert p["rule_enabled"] == 1
    templates = json.loads(p["templates"])
    assert templates == ["{Dạ chào|Hello} bạn nhé!"]


def test_google_sheet_wrong_tab_detection_and_invalid_delay():
    acc_id = db.upsert_account("Nick Minh", "10002")
    db.upsert_page("102938475612345", "Shop Thời Trang", "existing_token_xyz", acc_id)

    # Case 1: Google gviz returns the 'Pages' tab when requesting 'Comments' because 'Comments' was renamed
    mock_pages = [
        {"page id": "'102938475612345", "tên gợi nhớ": "Shop Thời Trang", "delay (phút)": "20", "bật tự động": "TRUE"}
    ]
    with patch("sheets_sync.fetch_sheet_csv") as mock_fetch:
        mock_fetch.side_effect = lambda sheet_id, tab: (mock_pages, None)
        res = sheets_sync.sync_from_google_sheet("https://docs.google.com/spreadsheets/d/mock123/edit")
        assert res["success"] is False
        assert "Comments" in res["error"] or "Mẫu bình luận" in res["error"]

    # Case 2: Invalid delay value ('abc') is reported in errors and not silently defaulted to 10
    mock_pages_bad_delay = [
        {"page id": "'102938475612345", "tên gợi nhớ": "Shop Thời Trang", "delay (phút)": "abc", "bật tự động": "TRUE"}
    ]
    mock_comments_ok = [
        {"page id": "'102938475612345", "mẫu bình luận": "Hello bạn", "bật": "TRUE"}
    ]
    with patch("sheets_sync.fetch_sheet_csv") as mock_fetch:
        def fetch_side_effect(sheet_id, tab):
            if tab == "Pages":
                return mock_pages_bad_delay, None
            return mock_comments_ok, None
        mock_fetch.side_effect = fetch_side_effect
        res2 = sheets_sync.sync_from_google_sheet("https://docs.google.com/spreadsheets/d/mock123/edit")
        assert res2["success"] is True
        assert res2["pages_synced"] == 0
        assert len(res2["errors"]) == 1
        assert "Delay" in res2["errors"][0]


def test_google_sheet_scientific_notation_rejection():
    clean_id, err = sheets_sync.validate_page_id("1.02938E+14")
    assert clean_id is None
    assert "hàm mũ (E+)" in err

    clean_id2, err2 = sheets_sync.validate_page_id("'102938475612345")
    assert clean_id2 == "102938475612345"
    assert err2 is None


def test_startup_vs_runtime_stale_task_recovery():
    acc_id = db.upsert_account("Nick 3")
    db.upsert_page("page_rec", "Page Recovery", "token_rec", acc_id)
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    # Create two tasks: one just locked (active in-flight), one locked 10 minutes ago (stale)
    db.create_task("page_rec", "post_fresh", "Fresh content", now_iso)
    db.create_task("page_rec", "post_stale", "Stale content", now_iso)
    tasks = {t["post_id"]: t["id"] for t in db.get_all_tasks(page_id="page_rec")}

    conn = db.get_connection()
    with conn:
        conn.execute(
            "UPDATE tasks SET status = 'SENDING', locked_at = ? WHERE id = ?",
            (now_iso, tasks["post_fresh"])
        )
        conn.execute(
            "UPDATE tasks SET status = 'SENDING', locked_at = ? WHERE id = ?",
            ((now_utc - timedelta(minutes=10)).isoformat(), tasks["post_stale"])
        )
    conn.close()

    # Runtime recovery (startup=False) MUST ONLY touch the stale task, leaving fresh task in SENDING!
    recovered_count = db.recover_stale_tasks(startup=False, stale_after_seconds=300)
    assert recovered_count == 1

    after_runtime = {t["post_id"]: t for t in db.get_all_tasks(page_id="page_rec")}
    assert after_runtime["post_fresh"]["status"] == "SENDING"
    assert after_runtime["post_stale"]["status"] == "NEED_CHECK"

    # Startup recovery (startup=True) recovers all remaining SENDING tasks
    db.recover_stale_tasks(startup=True)
    after_startup = {t["post_id"]: t for t in db.get_all_tasks(page_id="page_rec")}
    assert after_startup["post_fresh"]["status"] == "NEED_CHECK"


def test_min_gap_and_daily_limit_same_batch():
    acc_id = db.upsert_account("Nick Gap")
    db.upsert_page("page_gap", "Page Gap", "token_gap", acc_id)
    db.update_page_rule("page_gap", delay_minutes=5, templates=["Hi"], max_comments_per_day=2, is_enabled=True)

    past_iso = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    # Create 2 due tasks for the same page in the same batch
    db.create_task("page_gap", "post_g1", "Msg 1", past_iso)
    db.create_task("page_gap", "post_g2", "Msg 2", past_iso)

    with patch("meta_client.post_comment") as mock_post:
        mock_post.return_value = {"success": True, "comment_id": "c_1"}
        scheduler.execute_due_tasks()
        # Only 1 comment should have been sent because of MIN_GAP_PER_PAGE_SEC (45s)!
        assert mock_post.call_count == 1

    tasks = {t["post_id"]: t for t in db.get_all_tasks(page_id="page_gap")}
    assert tasks["post_g1"]["status"] == "SUCCESS"
    assert tasks["post_g2"]["status"] == "PENDING"


def test_debug_token_failure_does_not_disable_pages():
    acc_id = db.upsert_account("Nick Health")
    db.upsert_page("page_h1", "Page Health", "token_h1", acc_id)
    db.create_task("page_h1", "post_h1", "Hello", datetime.now(timezone.utc).isoformat())

    # 1. When debug_token fails to check (e.g. missing app credentials or network error -> checked=False)
    with patch("meta_client.debug_token") as mock_dbg:
        mock_dbg.return_value = {"checked": False, "is_valid": None, "status": "UNKNOWN"}
        scheduler.run_maintenance_cycle()

    p = db.get_page("page_h1")
    assert p["is_active"] == 1
    assert p["token_status"] == "OK"

    # 2. When debug_token explicitly confirms token is invalid (checked=True, is_valid=False)
    with patch("meta_client.debug_token") as mock_dbg:
        mock_dbg.return_value = {"checked": True, "is_valid": False, "status": "EXPIRED", "missing_scopes": []}
        scheduler.run_maintenance_cycle()

    p_after = db.get_page("page_h1")
    assert p_after["is_active"] == 0
    assert p_after["token_status"] == "EXPIRED"
    # Pending task for that page should now be PAUSED
    t_after = db.get_all_tasks(page_id="page_h1")[0]
    assert t_after["status"] == "PAUSED"


def test_retry_and_resolve_task_status_guards():
    acc_id = db.upsert_account("Nick Guard")
    db.upsert_page("page_grd", "Page Guard", "token_grd", acc_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    db.create_task("page_grd", "post_ok", "Already posted", now_iso)
    task_id = db.get_all_tasks(page_id="page_grd")[0]["id"]

    # Mark task as SUCCESS
    db.update_task_result(task_id, "SUCCESS", comment_id="fb_c_999")

    # Attempting to retry a SUCCESS task MUST fail (return False and HTTP 400)
    assert db.retry_task(task_id) is False
    with TestClient(fastapi_app.app) as client:
        resp = client.post(f"/api/tasks/{task_id}/retry")
        assert resp.status_code == 400
        assert "Đã đăng" in resp.json()["detail"]


def test_scheduler_post_belongs_verification_and_pfbid_url():
    # Verify URL extraction supports pfbid
    extracted = meta_client.extract_post_id_from_input(
        "https://www.facebook.com/MyPage/posts/pfbid02abcXYZ123?__cft__[0]=1",
        "111222"
    )
    assert extracted == "111222_pfbid02abcXYZ123"

    # Attempting to post to a post that belongs to a different page must be rejected
    with patch("httpx.Client.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "id": "post_foreign_99",
            "from": {"id": "foreign_page_888"}
        }
        mock_get.return_value = mock_resp

        with pytest.raises(meta_client.MetaAPIError) as exc_info:
            meta_client.verify_post_belongs_to_page("post_foreign_99", "my_page_111", "token")
        assert "không thuộc về Page này" in str(exc_info.value)


def test_timeout_self_reconciliation():
    # When post_comment times out, it checks if the comment actually landed on Facebook
    with patch("httpx.Client.post", side_effect=httpx.ReadTimeout("Timed out")):
        with patch("meta_client.check_comment_exists", return_value="reconciled_comment_123"):
            res = meta_client.post_comment("111_222", "111", "token", "Nội dung test")
            assert res["success"] is True
            assert res["comment_id"] == "reconciled_comment_123"

        with patch("meta_client.check_comment_exists", return_value=None):
            res2 = meta_client.post_comment("111_222", "111", "token", "Nội dung test")
            assert res2["success"] is False
            assert res2["category"] == "NEED_CHECK"


def test_antidetect_hybrid_linking_and_cdp_execution():
    import antidetect_client
    acc_id = db.upsert_account("Nick Graph API", "u_graph_1", "tok_u")
    # 1. Existing Graph API page
    db.upsert_page("993921657144854", "Chị Đẹp Thích Vẽ", "valid_page_token", acc_id, token_status="OK")

    # 2. Syncing matching page from Antidetect upgrades it to HYBRID without losing its token
    res_hybrid = db.upsert_antidetect_page(
        fb_page_id="61575475678031",
        page_name="Chị Đẹp Thích Vẽ",
        page_url="https://www.facebook.com/61575475678031",
        antidetect_profile_id="prof-uuid-2",
        antidetect_profile_name="Profile 2 - Bóng Ma Đề Cương",
        account_id=acc_id,
    )
    assert res_hybrid["action"] == "linked_hybrid"
    assert res_hybrid["page_id"] == "993921657144854"

    p_hybrid = db.get_page_with_decrypted_token("993921657144854")
    assert p_hybrid["connection_mode"] == "HYBRID"
    assert p_hybrid["page_access_token"] == "valid_page_token"
    assert p_hybrid["fb_profile_id"] == "61575475678031"

    # 3. Syncing a new Antidetect-only page (no Graph API token)
    res_ad = db.upsert_antidetect_page(
        fb_page_id="61594671061927",
        page_name="Tiện Ích Ngôi Nhà",
        page_url="https://www.facebook.com/61594671061927",
        antidetect_profile_id="prof-uuid-7",
        antidetect_profile_name="Profile 7 - Meo Meo",
        account_id=acc_id,
    )
    assert res_ad["action"] == "inserted"
    p_ad = db.get_page("61594671061927")
    assert p_ad["connection_mode"] == "ANTIDETECT"
    assert p_ad["token_status"] == "ANTIDETECT_READY"

    # 4. Manual task creation on Antidetect-only page works without Graph API token
    with TestClient(fastapi_app.app) as client:
        resp = client.post("/api/tasks/manual", json={
            "page_id": "61594671061927",
            "post_id_or_url": "https://www.facebook.com/61594671061927/posts/pfbid02xyzAntidetectPost",
            "content": "{Chào|Xin chào} bạn nhé!",
            "post_now": True,
            "batch_id": "batch_ad_1"
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    # 5. Executing due task for Antidetect-only page routes to antidetect_client.post_comment_cdp
    with patch("antidetect_client.post_comment_cdp") as mock_cdp:
        mock_cdp.return_value = {
            "success": True,
            "comment_id": "cdp_61594671061927_999",
            "mode": "ANTIDETECT_CDP",
            "usage_pct": None,
        }
        scheduler.execute_due_tasks()
        assert mock_cdp.call_count == 1
        tasks = db.get_all_tasks(page_id="61594671061927")
        assert tasks[0]["status"] == "SUCCESS"
        assert tasks[0]["comment_id"] == "cdp_61594671061927_999"

