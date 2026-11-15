"""Dashboard transport selection and browser dispatch boundaries; no live browser."""
from datetime import datetime, timezone
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

import app
import antidetect_client as browser
import comment_dispatch
import config
import database as db
import meta_client
import scheduler


@pytest.fixture
def browser_page():
    pid = "61570000000001"
    db.upsert_antidetect_page(pid, "Page thử", "https://www.facebook.com/" + pid,
                             "profile-one", "Profile 1", db.upsert_account("Profile 1"))
    db.set_page_send_mode(pid, "BROWSER")
    return pid


@pytest.fixture
def client():
    with TestClient(app.app, base_url="http://localhost",
                    headers={"X-Dashboard-Key": config.DASHBOARD_ADMIN_KEY}) as client:
        yield client


@pytest.fixture
def checked(browser_page, monkeypatch):
    monkeypatch.setattr(browser, "verify_page_connection_cdp", lambda page: {
        "success": True, "browser_status": "READY", "error": None,
        "identity_id": browser_page, "profile_id": "profile-one"})

    def no_graph(*args, **kwargs):
        raise AssertionError("Browser Page must not use Graph API")

    monkeypatch.setattr(meta_client, "verify_page_connection", no_graph)
    monkeypatch.setattr(meta_client, "get_recent_posts", no_graph)
    monkeypatch.setattr(meta_client, "verify_post_belongs_to_page", no_graph)
    return browser_page


def test_browser_check_and_list_without_token(client, checked):
    before = client.get("/api/pages").json()["pages"][0]
    assert not before["send_ready"]
    result = client.post(f"/api/pages/{checked}/check")
    assert result.status_code == 200
    assert result.json()["browser_status"] == "READY"
    page = client.get("/api/pages").json()["pages"][0]
    assert page["browser_ready"] and page["send_ready"]
    assert not page["graph_ready"] and not page["has_page_token"]
    assert "page_access_token_enc" not in page


def test_browser_manual_verifies_and_queues_without_sending(client, checked, monkeypatch):
    assert client.post(f"/api/pages/{checked}/check").status_code == 200
    target = checked + "_12345"
    calls = []

    def verify(page, post, post_url=None):
        calls.append((page["page_id"], post))
        return {"clean_post_id": target, "permalink_url": "https://www.facebook.com/" + target}

    monkeypatch.setattr(browser, "verify_post_for_page_cdp", verify)
    monkeypatch.setattr(browser, "send_page_comment_cdp", lambda *a, **kw: pytest.fail("API must only queue"))
    body = {"page_id": checked, "post_id_or_url": target, "content": "Câu đã xác nhận", "batch_id": "api-batch"}
    result = client.post("/api/tasks/manual", json=body)
    assert result.status_code == 200, result.text
    assert calls == [(checked, target)]
    task = db.get_all_tasks()[0]
    assert task["status"] == "PENDING" and task["comment_content"] == body["content"]
    assert client.post("/api/tasks/manual", json=body).status_code == 400


def test_browser_recent_posts_uses_selected_transport(client, checked, monkeypatch):
    assert client.post(f"/api/pages/{checked}/check").status_code == 200
    monkeypatch.setattr(browser, "get_recent_posts_for_page_cdp", lambda page: [{"id": checked + "_12345"}])
    result = client.get(f"/api/pages/{checked}/recent_posts")
    assert result.status_code == 200 and result.json()["source"] == "BROWSER"


def test_stale_browser_check_cannot_authorize_different_profile(client, browser_page, monkeypatch):
    monkeypatch.setattr(browser, "verify_page_connection_cdp", lambda page: {
        "success": True, "browser_status": "READY", "identity_id": browser_page,
        "profile_id": "different-profile"})
    assert client.post(f"/api/pages/{browser_page}/check").status_code == 409
    assert not db.get_page(browser_page)["browser_checked_at"]


def test_mode_change_clears_browser_check_and_does_not_resume(client, checked):
    assert client.post(f"/api/pages/{checked}/check").status_code == 200
    db.create_task(checked, checked + "_12345", "Câu chờ", datetime.now(timezone.utc).isoformat(), "MANUAL", "mode")
    result = client.post(f"/api/pages/{checked}/send_mode", json={"send_mode": "GRAPH_API"})
    assert result.status_code == 200
    assert db.get_all_tasks()[0]["status"] == "PAUSED"
    page = client.get("/api/pages").json()["pages"][0]
    assert page["send_mode"] == "GRAPH_API" and not page["send_ready"]
    assert page["browser_status"] == "UNCHECKED"


def test_busy_page_rejects_configuration_but_allows_stop(client, browser_page):
    with comment_dispatch.page_lock(browser_page):
        assert client.post(f"/api/pages/{browser_page}/send_mode", json={"send_mode": "GRAPH_API"}).status_code == 409
        assert client.post(f"/api/pages/{browser_page}/check").status_code == 409
        assert client.post(f"/api/pages/{browser_page}/automation", json={"enabled": False}).status_code == 200


def test_browser_check_login_preserves_graph_status(client, browser_page, monkeypatch):
    original = db.get_page(browser_page)["token_status"]
    monkeypatch.setattr(browser, "verify_page_connection_cdp", lambda page: {
        "success": False, "browser_status": "LOGIN_REQUIRED", "error": "Cần đăng nhập Facebook",
        "identity_id": browser_page, "profile_id": "profile-one"})
    result = client.post(f"/api/pages/{browser_page}/check")
    assert result.status_code == 409
    page = db.get_page(browser_page)
    assert page["browser_status"] == "LOGIN_REQUIRED" and page["token_status"] == original


def test_browser_reactivate_keeps_graph_token_state(client, checked):
    with closing(db.get_connection()) as conn, conn:
        conn.execute("UPDATE pages SET is_active=0, token_status='EXPIRED' WHERE page_id=?", (checked,))
    result = client.post(f"/api/pages/{checked}/reactivate")
    assert result.status_code == 200
    page = db.get_page(checked)
    assert page["is_active"] and page["token_status"] == "EXPIRED"
    assert page["browser_status"] == "READY"


def test_alias_cannot_bypass_canonical_page_guard_or_create_orphan_task(client, checked):
    alias = "61570000000002"
    with closing(db.get_connection()) as conn, conn:
        conn.execute("UPDATE pages SET fb_profile_id=? WHERE page_id=?", (alias, checked))
    for action, payload in (("send_mode", {"send_mode": "BROWSER"}), ("check", None),
                            ("automation", {"enabled": False}), ("reactivate", None),
                            ("resume_tasks", None)):
        assert client.post(f"/api/pages/{alias}/{action}", json=payload).status_code == 404
    assert client.get(f"/api/pages/{alias}/recent_posts").status_code == 404
    assert client.post("/api/tasks/manual", json={"page_id": alias,
        "post_id_or_url": checked + "_12345", "content": "Must not queue"}).status_code == 404
    assert db.get_all_tasks() == []


def test_read_auth_failure_invalidates_browser_readiness_immediately(client, checked, monkeypatch):
    assert client.post(f"/api/pages/{checked}/check").status_code == 200

    def logged_out(page):
        raise browser.BrowserAutomationError("Cần đăng nhập", "TOKEN_EXPIRED", "LOGIN_REQUIRED")

    monkeypatch.setattr(browser, "get_recent_posts_for_page_cdp", logged_out)
    assert client.get(f"/api/pages/{checked}/recent_posts").status_code == 409
    assert db.get_page(checked)["browser_status"] == "LOGIN_REQUIRED"
