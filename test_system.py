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
    scheduler.SCHEDULER_METRICS["paused_all"] = False
    yield
    scheduler.SCHEDULER_METRICS["paused_all"] = False


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


def test_reactivate_page_endpoint_hybrid_and_graph_guard():
    acc_id = db.upsert_account("Nick Reactivate", "u_reactivate", "user_tok")
    db.upsert_page("page_rx_hybrid", "Page Reactivate Hybrid", "page_tok_hybrid", acc_id, token_status="OK")
    linked = db.upsert_antidetect_page(
        fb_page_id="fb_rx_hybrid",
        page_name="Page Reactivate Hybrid",
        page_url="https://www.facebook.com/fb_rx_hybrid",
        antidetect_profile_id="prof-rx",
        antidetect_profile_name="Profile Reactivate",
        account_id=acc_id,
    )
    assert linked["action"] == "linked_hybrid"
    db.update_page_status("page_rx_hybrid", False, "EXPIRED", "Token Graph hết hạn")
    assert db.get_page("page_rx_hybrid")["is_active"] == 0

    db.upsert_page("page_rx_graph", "Page Reactivate Graph", "page_tok_graph", acc_id, token_status="EXPIRED")
    assert db.get_page("page_rx_graph")["is_active"] == 0

    db.upsert_page("page_rx_manual", "Page Reactivate Manual", "page_tok_manual", acc_id)
    db.update_page_status("page_rx_manual", False, "OK", "Tạm khóa thủ công")

    with TestClient(fastapi_app.app) as client:
        # HYBRID + token chết: kích hoạt lại và chuyển hẳn sang CDP
        resp = client.post("/api/pages/page_rx_hybrid/reactivate")
        assert resp.status_code == 200
        assert resp.json()["token_status"] == "ANTIDETECT_READY"
        p = db.get_page("page_rx_hybrid")
        assert p["is_active"] == 1
        assert p["token_status"] == "ANTIDETECT_READY"

        # Graph-only + token chết: chặn, giữ nguyên khóa
        resp = client.post("/api/pages/page_rx_graph/reactivate")
        assert resp.status_code == 400
        assert "Profile Antidetect" in resp.json()["detail"]
        assert db.get_page("page_rx_graph")["is_active"] == 0

        # Page bị khóa thủ công dù token OK: kích hoạt lại, giữ nguyên token_status
        resp = client.post("/api/pages/page_rx_manual/reactivate")
        assert resp.status_code == 200
        p = db.get_page("page_rx_manual")
        assert p["is_active"] == 1
        assert p["token_status"] == "OK"

        # Gọi lại lần hai vẫn an toàn (idempotent)
        resp = client.post("/api/pages/page_rx_manual/reactivate")
        assert resp.status_code == 200
        assert resp.json()["is_active"] is True

        # Page không tồn tại
        resp = client.post("/api/pages/khong_ton_tai/reactivate")
        assert resp.status_code == 404


def test_manual_resolve_updates_page_counters():
    acc_id = db.upsert_account("Nick Manual Resolve")
    db.upsert_page("page_rsv", "Page Resolve", "token_rsv", acc_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    db.create_task("page_rsv", "post_rsv", "Msg", now_iso)
    task_id = db.get_all_tasks(page_id="page_rsv")[0]["id"]

    db.update_task_result(task_id, "NEED_CHECK", error_message="Chưa xác định được kết quả")
    assert db.get_page("page_rsv")["comments_today_count"] == 0

    assert db.resolve_task(task_id, comment_id="verified_by_operator") is True
    page = db.get_page("page_rsv")
    assert page["comments_today_count"] == 1
    assert page["last_comment_at"] is not None
    assert page["comments_today_date"] == db.local_day_key(datetime.now(timezone.utc))


def test_local_day_counter_and_reset_boundary(monkeypatch):
    monkeypatch.setattr(config, "DAILY_COUNTER_UTC_OFFSET_HOURS", 7)
    before_midnight = datetime(2026, 11, 8, 16, 59, tzinfo=timezone.utc)
    at_midnight = datetime(2026, 11, 8, 17, 0, tzinfo=timezone.utc)

    assert db.local_day_key(before_midnight) == "2026-11-08"
    assert db.next_daily_reset_utc(before_midnight) == "2026-11-08T17:00:00+00:00"
    assert db.local_day_key(at_midnight) == "2026-11-09"
    assert db.next_daily_reset_utc(at_midnight) == "2026-11-09T17:00:00+00:00"


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


def test_hybrid_scanner_falls_back_to_cdp_after_token_expiry(monkeypatch):
    acc_id = db.upsert_account("Nick Hybrid Scan", "u_hscan", "user_tok")
    db.upsert_page("page_hscan", "Page Hybrid Scan", "page_tok_hscan", acc_id, token_status="OK")
    linked = db.upsert_antidetect_page(
        fb_page_id="fb_hscan",
        page_name="Page Hybrid Scan",
        page_url="https://www.facebook.com/fb_hscan",
        antidetect_profile_id="prof-hscan",
        antidetect_profile_name="Profile Hybrid Scan",
        account_id=acc_id,
    )
    assert linked["action"] == "linked_hybrid"
    monkeypatch.setattr(config, "ANTIDETECT_AUTO_SCAN_POSTS", True)
    fresh_time = datetime.now(timezone.utc).isoformat()

    with patch("antidetect_client._read_runtime_readonly", return_value={}), \
         patch("antidetect_client._probe_cdp_port", return_value=False), \
         patch(
             "meta_client.get_recent_posts",
             side_effect=meta_client.MetaAPIError(190, 463, "expired", "TOKEN_EXPIRED"),
         ) as mock_graph, \
         patch("antidetect_client.get_recent_posts_cdp", return_value=[{
             "id": "post_hscan_1",
             "created_time": fresh_time,
             "permalink_url": "https://www.facebook.com/fb_hscan/posts/1",
         }]) as mock_cdp:
        scheduler.detect_new_posts_and_schedule()

    mock_graph.assert_called_once()
    mock_cdp.assert_called_once()
    page = db.get_page("page_hscan")
    assert page["is_active"] == 1
    assert page["token_status"] == "ANTIDETECT_READY"
    tasks = db.get_all_tasks(page_id="page_hscan")
    assert any(t["post_id"] == "post_hscan_1" for t in tasks)


def test_single_flight_executor_skips_overlapping_run():
    acc_id = db.upsert_account("Nick Single Flight")
    db.upsert_page("page_sf", "Page Single Flight", "token_sf", acc_id)
    past_iso = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    db.create_task("page_sf", "post_sf", "Msg", past_iso)

    assert scheduler._EXEC_LOCK.acquire(blocking=False)
    try:
        with patch("meta_client.post_comment") as mock_post:
            scheduler.execute_due_tasks()
            mock_post.assert_not_called()
    finally:
        scheduler._EXEC_LOCK.release()

    assert db.get_all_tasks(page_id="page_sf")[0]["status"] == "PENDING"

    with patch("meta_client.post_comment") as mock_post:
        mock_post.return_value = {"success": True, "comment_id": "c_sf"}
        scheduler.execute_due_tasks()
        mock_post.assert_called_once()


def test_google_sheet_unified_single_tab_mode():
    acc_id = db.upsert_account("Nick Test Unified", "u_uni", "tok")
    db.upsert_page("61594671061927", "Tiện Ích Ngôi Nhà", "tok_p", acc_id)

    unified_rows = [
        {
            "page id": "'61594671061927",
            "tên fanpage": "Tiện Ích Ngôi Nhà",
            "sau bao lâu comment (phút)": "15",
            "mỗi ngày mấy comment": "12",
            "mẫu bình luận": "{Chào|Xin chào} bạn, mẫu 1!",
            "bật tự động": "TRUE",
        },
        {
            "page id": "'61594671061927",
            "tên fanpage": "Tiện Ích Ngôi Nhà",
            "sau bao lâu comment (phút)": "15",
            "mỗi ngày mấy comment": "12",
            "mẫu bình luận": "Mẫu 2 {nhé|nha}!",
            "bật tự động": "TRUE",
        },
    ]

    with patch("sheets_sync.fetch_sheet_csv", return_value=(unified_rows, None)):
        res = sheets_sync.sync_from_google_sheet(
            "https://docs.google.com/spreadsheets/d/1Qq1lkhVzlDe0FJ5k3HDDkBiGYDqTItO2ycQMDQHATD0/edit?pli=1&gid=1405549804#gid=1405549804"
        )
        assert res["success"] is True
        assert res["pages_synced"] == 1
        assert "Chế độ 1 Bảng gộp" in res["message"]

    p = db.get_page("61594671061927")
    assert p["delay_minutes"] == 15
    assert p["max_comments_per_day"] == 12
    assert json.loads(p["templates"]) == ["{Chào|Xin chào} bạn, mẫu 1!", "Mẫu 2 {nhé|nha}!"]


def test_maintenance_skips_auto_sheet_sync_while_paused(monkeypatch):
    monkeypatch.setitem(
        scheduler.SCHEDULER_METRICS["sheet_sync"],
        "sheet_url",
        "https://docs.google.com/spreadsheets/d/mock123/edit",
    )
    with patch("sheets_sync.sync_from_google_sheet") as mock_sync:
        monkeypatch.setitem(scheduler.SCHEDULER_METRICS, "paused_all", True)
        scheduler.run_maintenance_cycle()
        mock_sync.assert_not_called()

        monkeypatch.setitem(scheduler.SCHEDULER_METRICS, "paused_all", False)
        mock_sync.return_value = {"success": True}
        scheduler.run_maintenance_cycle()
        mock_sync.assert_called_once()


def test_manual_sync_endpoint_schedules_background_cycle(monkeypatch):
    monkeypatch.setitem(scheduler.SCHEDULER_METRICS, "paused_all", False)
    with TestClient(fastapi_app.app) as client:
        with patch("scheduler.run_manual_sync_cycle") as mock_cycle:
            resp = client.post("/api/sync")
            assert resp.status_code == 200
            assert "bắt đầu" in resp.json()["message"]
            mock_cycle.assert_called_once()


def test_manual_sync_blocked_while_paused(monkeypatch):
    monkeypatch.setitem(scheduler.SCHEDULER_METRICS, "paused_all", True)
    with TestClient(fastapi_app.app) as client:
        with patch("scheduler.run_manual_sync_cycle") as mock_cycle:
            resp = client.post("/api/sync")
            assert resp.status_code == 409
            assert "tạm dừng" in resp.json()["detail"].lower()
            mock_cycle.assert_not_called()


def test_manual_sync_cycle_honors_pause(monkeypatch):
    acc_id = db.upsert_account("Nick Manual Pause")
    db.upsert_page("page_mp", "Page Manual Pause", "token_mp", acc_id)
    past_iso = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    db.create_task("page_mp", "post_mp", "Msg", past_iso)

    monkeypatch.setitem(scheduler.SCHEDULER_METRICS, "paused_all", True)
    with patch("meta_client.post_comment") as mock_post:
        scheduler.run_manual_sync_cycle()
        scheduler.execute_due_tasks()
        mock_post.assert_not_called()

    assert db.get_all_tasks(page_id="page_mp")[0]["status"] == "PENDING"


def test_pause_stops_batch_before_next_task(monkeypatch):
    acc_id = db.upsert_account("Nick Pause Batch")
    db.upsert_page("page_pa", "Page Pause A", "token_pa", acc_id)
    db.upsert_page("page_pb", "Page Pause B", "token_pb", acc_id)
    past_iso = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    db.create_task("page_pa", "post_pa", "Msg A", past_iso)
    db.create_task("page_pb", "post_pb", "Msg B", past_iso)

    posted = []

    def fake_post(post_id, page_id, page_access_token, message):
        posted.append(page_id)
        monkeypatch.setitem(scheduler.SCHEDULER_METRICS, "paused_all", True)
        return {"success": True, "comment_id": f"c_{post_id}", "usage_pct": None}

    with patch("meta_client.post_comment", side_effect=fake_post):
        scheduler.execute_due_tasks()

    assert len(posted) == 1
    statuses = sorted(t["status"] for t in db.get_all_tasks())
    assert statuses == ["PENDING", "SUCCESS"]


def test_pause_after_lock_releases_task(monkeypatch):
    acc_id = db.upsert_account("Nick Pause Lock")
    db.upsert_page("page_pl", "Page Pause Lock", "token_pl", acc_id)
    past_iso = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    db.create_task("page_pl", "post_pl", "Msg", past_iso)

    real_lock = db.lock_task_for_sending

    def lock_then_pause(task_id):
        locked = real_lock(task_id)
        monkeypatch.setitem(scheduler.SCHEDULER_METRICS, "paused_all", True)
        return locked

    monkeypatch.setattr(db, "lock_task_for_sending", lock_then_pause)
    with patch("meta_client.post_comment") as mock_post:
        scheduler.execute_due_tasks()
        mock_post.assert_not_called()

    task = db.get_all_tasks(page_id="page_pl")[0]
    assert task["status"] == "PENDING"
    assert task["locked_at"] is None


def test_manual_sync_skips_execution_if_paused_during_scan():
    def fake_scan():
        scheduler.SCHEDULER_METRICS["paused_all"] = True

    with patch.object(scheduler, "_detect_new_posts_and_schedule", side_effect=fake_scan):
        with patch.object(scheduler, "_execute_due_tasks") as mock_exec:
            scheduler.run_manual_sync_cycle()
            mock_exec.assert_not_called()


def test_maintenance_keeps_hybrid_page_active_on_cdp(monkeypatch):
    monkeypatch.setitem(scheduler.SCHEDULER_METRICS["sheet_sync"], "sheet_url", "")
    acc_id = db.upsert_account("Nick Maint Hybrid", "u_mh", "tok")
    db.upsert_page("page_mh", "Page Maint Hybrid", "token_mh", acc_id, token_status="OK")
    db.upsert_antidetect_page(
        fb_page_id="fb_mh",
        page_name="Page Maint Hybrid",
        page_url="https://www.facebook.com/fb_mh",
        antidetect_profile_id="prof_mh",
        antidetect_profile_name="Profile Maint Hybrid",
        account_id=acc_id,
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    db.create_task("page_mh", "post_mh", "Msg", now_iso)

    with patch(
        "meta_client.debug_token",
        return_value={"checked": True, "is_valid": False, "status": "EXPIRED", "missing_scopes": []},
    ):
        scheduler.run_maintenance_cycle()

    page = db.get_page("page_mh")
    assert page["is_active"] == 1
    assert page["token_status"] == "ANTIDETECT_READY"
    assert db.get_all_tasks(page_id="page_mh")[0]["status"] == "PENDING"


def test_disabling_auto_rule_cancels_pending_auto_only():
    acc_id = db.upsert_account("Nick Kill Switch")
    db.upsert_page("page_ks", "Page Kill Switch", "token_ks", acc_id)
    past_iso = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    db.create_task("page_ks", "post_auto_ks", "Auto message", past_iso, source="AUTO")
    db.create_task("page_ks", "post_manual_ks", "Manual message", past_iso, source="MANUAL", batch_id="b_ks")

    cancelled = db.update_page_rule("page_ks", 10, ["Mẫu"], 20, False)
    assert cancelled == 1

    tasks = {t["source"]: t for t in db.get_all_tasks(page_id="page_ks")}
    assert tasks["AUTO"]["status"] == "CANCELLED"
    assert tasks["MANUAL"]["status"] == "PENDING"

    with patch("meta_client.post_comment", return_value={"success": True, "comment_id": "c_manual_ks"}) as mock_post:
        scheduler.execute_due_tasks()
        assert mock_post.call_count == 1

    tasks = {t["source"]: t for t in db.get_all_tasks(page_id="page_ks")}
    assert tasks["AUTO"]["status"] == "CANCELLED"
    assert tasks["MANUAL"]["status"] == "SUCCESS"


def _mock_cdp_environment(monkeypatch):
    import antidetect_client

    class FakeWebSocket:
        pass

    class FakeConnect:
        async def __aenter__(self):
            return FakeWebSocket()

        async def __aexit__(self, *exc):
            return False

    async def fake_cdp_call(ws, msg_id, method, params=None, timeout=10.0):
        return {"id": msg_id, "result": {}}

    async def fake_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(antidetect_client, "acquire_profile_cdp_port", lambda pid: (9222, False, None))
    monkeypatch.setattr(antidetect_client, "_open_transient_tab", lambda port: ("t1", "ws://fake"))
    monkeypatch.setattr(antidetect_client, "_ensure_antidetect_sys_path", lambda: False)
    monkeypatch.setattr(antidetect_client, "_close_transient_tab", lambda *args: None)
    monkeypatch.setattr(antidetect_client, "release_profile_cdp_port", lambda *args: None)
    monkeypatch.setattr(antidetect_client, "_cdp_call", fake_cdp_call)
    monkeypatch.setattr(antidetect_client.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr("websockets.connect", lambda *args, **kwargs: FakeConnect())
    return antidetect_client


def _cdp_page_state(actor_names, actor_button=None, switch_button=None, comment_box=True):
    return {
        "url": "https://www.facebook.com/example/posts/1",
        "accessState": "ok",
        "actorNames": list(actor_names),
        "actorButtonRect": actor_button,
        "switchProfileButtonRect": switch_button,
        "commentBoxRect": {"x": 10, "y": 20, "w": 200, "h": 30} if comment_box else None,
        "commentBoxPlaceholder": "Bình luận dưới tên Page Mẫu",
    }


def test_cdp_comment_blocked_when_actor_not_confirmed(monkeypatch):
    ad = _mock_cdp_environment(monkeypatch)
    state = _cdp_page_state(["Nick Cá Nhân"])

    async def fake_eval(ws, msg_id, expression, timeout=10.0):
        return dict(state)

    typed = []

    async def fake_type(ws, msg_id, text, human_helper=None):
        typed.append(text)
        return msg_id + 1

    monkeypatch.setattr(ad, "_cdp_eval", fake_eval)
    monkeypatch.setattr(ad, "_human_type_and_submit_on_ws", fake_type)

    res = ad.post_comment_cdp(
        post_id="post_actor",
        page_id="page_actor",
        page_name="Page Mẫu",
        page_url=None,
        profile_id="prof_actor",
        message="Xin chào",
    )

    assert res["success"] is False
    assert res["category"] == "INVALID_TARGET"
    assert "tài khoản cá nhân" in res["error"]
    assert typed == []


def test_cdp_comment_sends_when_actor_confirmed(monkeypatch):
    ad = _mock_cdp_environment(monkeypatch)
    state = _cdp_page_state(["Page Mẫu"])
    calls = {"evaluations": 0}
    typed = []

    async def fake_eval(ws, msg_id, expression, timeout=10.0):
        calls["evaluations"] += 1
        return dict(state) if calls["evaluations"] == 1 else {"found": True}

    async def fake_click(ws, msg_id, x, y, human_helper=None):
        return msg_id + 1

    async def fake_type(ws, msg_id, text, human_helper=None):
        typed.append(text)
        return msg_id + 1

    monkeypatch.setattr(ad, "_cdp_eval", fake_eval)
    monkeypatch.setattr(ad, "_human_click_on_ws", fake_click)
    monkeypatch.setattr(ad, "_human_type_and_submit_on_ws", fake_type)

    res = ad.post_comment_cdp(
        post_id="post_actor",
        page_id="page_actor",
        page_name="Page Mẫu",
        page_url=None,
        profile_id="prof_actor",
        message="Xin chào",
    )

    assert res["success"] is True
    assert typed == ["Xin chào"]


def test_cdp_actor_name_matching_is_exact():
    import antidetect_client as ad

    assert ad._actor_names_match(["Page Mẫu"], "Page Mẫu") is True
    assert ad._actor_names_match(["  page   mẫu "], "Page Mẫu") is True
    assert ad._actor_names_match(["Page Mẫu Outlet"], "Page Mẫu") is False
    assert ad._actor_names_match(["Nick Cá Nhân"], "Page Mẫu") is False
    assert ad._actor_names_match([], "Page Mẫu") is False
    assert ad._actor_names_match(["Page Mẫu", "Page Mẫu Outlet"], "Page Mẫu") is False
    assert ad._actor_names_match(["Page Mẫu"], "") is False

    options = [
        {"text": "Page Mẫu", "x": 100, "y": 200},
        {"text": "Page Mẫu Outlet", "x": 100, "y": 260},
    ]
    matches = ad._select_exact_actor_option(options, "Page Mẫu")
    assert matches == [{"text": "Page Mẫu", "x": 100, "y": 200}]

    duplicate_options = options + [{"text": "page   mẫu", "x": 100, "y": 300}]
    assert len(ad._select_exact_actor_option(duplicate_options, "Page Mẫu")) == 2


def test_cdp_comment_blocks_prefix_actor_name(monkeypatch):
    ad = _mock_cdp_environment(monkeypatch)
    state = _cdp_page_state(["Page Mẫu Outlet"])

    async def fake_eval(ws, msg_id, expression, timeout=10.0):
        return dict(state)

    async def fake_type(ws, msg_id, text, human_helper=None):
        raise AssertionError("must never type when actor name only matches as a prefix")

    monkeypatch.setattr(ad, "_cdp_eval", fake_eval)
    monkeypatch.setattr(ad, "_human_type_and_submit_on_ws", fake_type)

    res = ad.post_comment_cdp(
        post_id="post_prefix",
        page_id="page_actor",
        page_name="Page Mẫu",
        page_url=None,
        profile_id="prof_actor",
        message="Xin chào",
    )

    assert res["success"] is False
    assert res["category"] == "INVALID_TARGET"
    assert "Page khác" in res["error"]


def test_cdp_comment_blocks_ambiguous_actor_menu(monkeypatch):
    ad = _mock_cdp_environment(monkeypatch)
    state = _cdp_page_state(["Nick Cá Nhân"], actor_button={"x": 5, "y": 6})
    menu_options = [
        {"text": "Page Mẫu", "x": 100, "y": 200},
        {"text": "Page Mẫu", "x": 100, "y": 320},
    ]
    calls = {"evaluations": 0}
    typed = []

    async def fake_eval(ws, msg_id, expression, timeout=10.0):
        calls["evaluations"] += 1
        if calls["evaluations"] == 1:
            return dict(state)
        if calls["evaluations"] == 2:
            return list(menu_options)
        return dict(state)

    async def fake_click(ws, msg_id, x, y, human_helper=None):
        return msg_id + 1

    async def fake_type(ws, msg_id, text, human_helper=None):
        typed.append(text)
        return msg_id + 1

    monkeypatch.setattr(ad, "_cdp_eval", fake_eval)
    monkeypatch.setattr(ad, "_human_click_on_ws", fake_click)
    monkeypatch.setattr(ad, "_human_type_and_submit_on_ws", fake_type)

    res = ad.post_comment_cdp(
        post_id="post_ambiguous",
        page_id="page_actor",
        page_name="Page Mẫu",
        page_url=None,
        profile_id="prof_actor",
        message="Xin chào",
    )

    assert res["success"] is False
    assert res["category"] == "INVALID_TARGET"
    assert "trùng" in res["error"]
    assert typed == []


def test_google_sheet_unified_disables_pages_missing_from_sheet():
    acc_id = db.upsert_account("Nick Unified Drop", "u_drop", "tok")
    db.upsert_page("100000000000001", "Page Keep Unified", "tok_keep", acc_id)
    db.upsert_page("100000000000002", "Page Drop Unified", "tok_drop", acc_id)

    unified_rows = [{
        "page id": "'100000000000001",
        "tên fanpage": "Page Keep Unified",
        "sau bao lâu comment (phút)": "10",
        "mỗi ngày mấy comment": "20",
        "mẫu bình luận": "Mẫu giữ lại",
        "bật tự động": "TRUE",
    }]

    with patch("sheets_sync.fetch_sheet_csv", return_value=(unified_rows, None)):
        res = sheets_sync.sync_from_google_sheet("https://docs.google.com/spreadsheets/d/mock123/edit")

    assert res["success"] is True
    assert res["pages_synced"] == 1
    assert res["pages_disabled"] == 1
    assert db.get_page("100000000000001")["rule_enabled"] == 1
    assert db.get_page("100000000000002")["rule_enabled"] == 0


def test_google_sheet_rejects_ambiguous_page_name():
    acc_id = db.upsert_account("Nick Ambiguous", "u_amb", "tok")
    db.upsert_page("100000000000003", "Page Trùng Tên", "tok_a", acc_id)
    db.upsert_page("100000000000004", "Page Trùng Tên", "tok_b", acc_id)
    db.update_page_rule("100000000000003", 10, ["Rule A"], 20, True)
    db.update_page_rule("100000000000004", 10, ["Rule B"], 20, True)

    unified_rows = [{
        "tên fanpage": "Page Trùng Tên",
        "mẫu bình luận": "Mẫu mới",
        "bật tự động": "TRUE",
    }]

    with patch("sheets_sync.fetch_sheet_csv", return_value=(unified_rows, None)):
        res = sheets_sync.sync_from_google_sheet("https://docs.google.com/spreadsheets/d/mock123/edit")

    assert res["success"] is True
    assert res["pages_synced"] == 0
    assert any("trùng" in e for e in res["errors"])
    assert json.loads(db.get_page("100000000000003")["templates"]) == ["Rule A"]
    assert json.loads(db.get_page("100000000000004")["templates"]) == ["Rule B"]


def test_sheet_sync_preserves_disabled_rule_when_enabled_column_absent():
    acc_id = db.upsert_account("Nick Keep Disabled", "u_kd", "tok")
    db.upsert_page("100000000000005", "Page Giữ Tắt", "tok_kd", acc_id)
    db.update_page_rule("100000000000005", 10, ["Mẫu cũ"], 20, False)

    unified_rows = [{
        "page id": "'100000000000005",
        "tên fanpage": "Page Giữ Tắt",
        "mẫu bình luận": "Mẫu mới",
    }]

    with patch("sheets_sync.fetch_sheet_csv", return_value=(unified_rows, None)):
        res = sheets_sync.sync_from_google_sheet("https://docs.google.com/spreadsheets/d/mock123/edit")

    assert res["success"] is True
    assert res["errors"] == []
    page = db.get_page("100000000000005")
    assert page["rule_enabled"] == 0
    assert json.loads(page["templates"]) == ["Mẫu mới"]


def test_sheet_sync_preserves_disabled_rule_when_enabled_cell_blank():
    acc_id = db.upsert_account("Nick Keep Blank", "u_kb", "tok")
    db.upsert_page("100000000000006", "Page Giữ Tắt 2", "tok_kb", acc_id)
    db.update_page_rule("100000000000006", 10, ["Mẫu cũ"], 20, False)

    unified_rows = [{
        "page id": "'100000000000006",
        "tên fanpage": "Page Giữ Tắt 2",
        "mẫu bình luận": "Mẫu mới",
        "bật tự động": "",
    }]

    with patch("sheets_sync.fetch_sheet_csv", return_value=(unified_rows, None)):
        res = sheets_sync.sync_from_google_sheet("https://docs.google.com/spreadsheets/d/mock123/edit")

    assert res["success"] is True
    assert res["errors"] == []
    assert db.get_page("100000000000006")["rule_enabled"] == 0


def test_sheet_sync_explicit_false_disables_rule():
    acc_id = db.upsert_account("Nick Explicit False", "u_ef", "tok")
    db.upsert_page("100000000000007", "Page Tắt Chủ Động", "tok_ef", acc_id)
    assert db.get_page("100000000000007")["rule_enabled"] == 1

    unified_rows = [{
        "page id": "'100000000000007",
        "tên fanpage": "Page Tắt Chủ Động",
        "mẫu bình luận": "Mẫu mới",
        "bật tự động": "FALSE",
    }]

    with patch("sheets_sync.fetch_sheet_csv", return_value=(unified_rows, None)):
        res = sheets_sync.sync_from_google_sheet("https://docs.google.com/spreadsheets/d/mock123/edit")

    assert res["success"] is True
    assert res["errors"] == []
    assert db.get_page("100000000000007")["rule_enabled"] == 0


