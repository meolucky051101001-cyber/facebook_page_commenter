"""Synthetic 19-HYBRID/47-Browser catalogue; no operator catalogue is read."""
from contextlib import closing
from datetime import datetime, timezone, timedelta

import pytest

import antidetect_client as browser
import comment_dispatch
import database as db
import meta_client
import scheduler


def queue_manual(page_id, suffix, minutes=1):
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    assert db.create_task(page_id, page_id + "_" + suffix, "Synthetic comment", when, source="MANUAL")


@pytest.fixture
def mixed_catalogue(monkeypatch):
    graph_pages, browser_pages = [], []
    providers = [db.upsert_account("Synthetic Graph provider A"), db.upsert_account("Synthetic Graph provider B")]
    for index in range(66):
        page_id = str(100000000010000 + index)
        profile_id = "synthetic-session-" + str(index // 10)
        if index < 19:
            db.upsert_page(page_id, "Synthetic HYBRID " + str(index), "synthetic-token-" + str(index), providers[index >= 9])
            db.upsert_antidetect_page(page_id, "Synthetic HYBRID " + str(index),
                                      "https://www.facebook.com/" + page_id, profile_id, "Synthetic profile", providers[index >= 9])
            db.update_page_connection_check(page_id, datetime.now(timezone.utc).isoformat(), token_status="OK")
            graph_pages.append(page_id)
        else:
            db.upsert_antidetect_page(page_id, "Synthetic Browser " + str(index),
                                      "https://www.facebook.com/" + page_id, profile_id, "Synthetic profile", providers[1])
            db.set_page_send_mode(page_id, "BROWSER")
            assert db.update_page_browser_check(page_id, browser_status="READY", identity_id=page_id, profile_id=profile_id)
            browser_pages.append(page_id)
    monkeypatch.setattr(scheduler, "comment_sheet_url", lambda: "")
    monkeypatch.setattr(meta_client, "verify_post_belongs_to_page", lambda post, *args: {"clean_post_id": post})
    return graph_pages, browser_pages


def test_catalogue_uses_explicit_transport_and_never_grants_auto_or_run_permission(mixed_catalogue, monkeypatch):
    graph_pages, browser_pages = mixed_catalogue
    assert len(graph_pages) == 19 and len(browser_pages) == 47
    pages = db.get_all_pages()
    assert sum(page["connection_mode"] == "HYBRID" for page in pages) == 19
    assert sum(page["connection_mode"] == "ANTIDETECT" for page in pages) == 47
    assert all(not page["rule_enabled"] for page in pages)
    for page_id in graph_pages:
        page = db.get_page_for_dispatch(page_id)
        assert page["send_mode"] == "GRAPH_API" and comment_dispatch.send_ready(page)
    for page_id in browser_pages:
        page = db.get_page_for_dispatch(page_id)
        assert page["send_mode"] == "BROWSER" and comment_dispatch.send_ready(page)
        assert not comment_dispatch.graph_ready(page)
    queue_manual(graph_pages[0], "101")
    queue_manual(browser_pages[0], "201")
    scheduler.SCHEDULER_METRICS["paused_all"] = True
    def unexpected(*args, **kwargs):
        pytest.fail("Paused catalogue caused external I/O")
    monkeypatch.setattr(meta_client, "post_comment", unexpected)
    monkeypatch.setattr(meta_client, "debug_token", unexpected)
    monkeypatch.setattr(browser, "send_page_comment_cdp", unexpected)
    monkeypatch.setattr(scheduler, "refresh_comment_sheet", unexpected)
    scheduler.execute_due_tasks()
    scheduler.run_maintenance_cycle()
    assert {task["status"] for task in db.get_all_tasks()} == {"PENDING"}


def test_mixed_queue_respects_selected_transport_without_graph_fallback(mixed_catalogue, monkeypatch):
    graph_pages, browser_pages = mixed_catalogue
    graph_id, browser_id = graph_pages[0], browser_pages[0]
    queue_manual(graph_id, "101", minutes=3)
    queue_manual(browser_id, "201", minutes=2)
    graph_calls, browser_calls = [], []
    def graph_send(post_id, page_id, *args):
        graph_calls.append(page_id)
        assert page_id == graph_id
        return {"success": False, "category": "TOKEN_EXPIRED", "error": "Synthetic expiration"}
    def browser_send(page, *args, before_submit, final_submit_gate, **kwargs):
        browser_calls.append(page["page_id"])
        assert page["page_id"] == browser_id
        assert not page.get("page_access_token")
        assert before_submit() and final_submit_gate()
        return {"success": True, "comment_id": "synthetic-browser-comment"}
    monkeypatch.setattr(meta_client, "post_comment", graph_send)
    monkeypatch.setattr(browser, "send_page_comment_cdp", browser_send)
    scheduler.execute_due_tasks()
    assert graph_calls == [graph_id]
    assert browser_calls == [browser_id]
    assert db.get_all_tasks(page_id=graph_id)[0]["status"] == "PAUSED"
    assert db.get_all_tasks(page_id=browser_id)[0]["status"] == "SUCCESS"
