import os
import json
import sqlite3
from datetime import datetime, timezone, timedelta
import pytest
from unittest.mock import patch, MagicMock

import config
import crypto
import database as db
import spintax
import scheduler
import meta_client
import sheets_sync

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

def test_google_sheet_sync_safe_no_token():
    acc_id = db.upsert_account("Nick Minh", "10002")
    db.upsert_page("102938475612345", "Shop Thời Trang", "existing_token_xyz", acc_id)

    # Mock sheet CSV with NO TOKEN column
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

    p = db.get_page("102938475612345")
    assert p["delay_minutes"] == 25
    assert p["max_comments_per_day"] == 15
    assert p["rule_enabled"] == 1
    templates = json.loads(p["templates"])
    assert templates == ["{Dạ chào|Hello} bạn nhé!"]

def test_google_sheet_scientific_notation_rejection():
    # Should cleanly reject 1.02938E+14
    clean_id, err = sheets_sync.validate_page_id("1.02938E+14")
    assert clean_id is None
    assert "hàm mũ (E+)" in err

    # Should accept valid quoted id
    clean_id2, err2 = sheets_sync.validate_page_id("'102938475612345")
    assert clean_id2 == "102938475612345"
    assert err2 is None

def test_startup_stale_task_recovery():
    acc_id = db.upsert_account("Nick 3")
    db.upsert_page("page_rec", "Page Recovery", "token_rec", acc_id)
    now_iso = datetime.now(timezone.utc).isoformat()

    db.create_task("page_rec", "post_rec_1", "Content 1", now_iso)
    tasks = db.get_all_tasks(page_id="page_rec")
    task_id = tasks[0]["id"]

    # Simulate task being stuck in SENDING when server killed
    conn = db.get_connection()
    with conn:
        conn.execute("UPDATE tasks SET status = 'SENDING' WHERE id = ?", (task_id,))
    conn.close()

    # Recovery function runs on startup
    db.recover_stale_tasks()

    recovered_task = db.get_all_tasks(page_id="page_rec")[0]
    assert recovered_task["status"] == "NEED_CHECK"
    assert "gián đoạn khi đang gửi" in recovered_task["error_message"]

def test_scheduler_post_belongs_verification():
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
