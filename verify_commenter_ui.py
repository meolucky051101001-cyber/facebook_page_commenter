"""Optional Playwright UI verification using intercepted synthetic responses only.

Run with an installed Playwright Chromium runtime. No application is started;
every browser request is intercepted and unknown paths are aborted.
"""
import copy
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
WEB = ROOT / "integration" / "antidetect" / "web"
OUTPUT = ROOT / ".pytest_cache" / "commenter-ui"


def main():
    pages = []
    profiles = []
    for index in range(66):
        browser = index >= 19
        profile_id = f"synthetic-{(index - 19) // 4}" if browser else None
        pages.append({
            "page_id": str(61570000000000 + index), "page_name": f"Synthetic Page {index + 1:02}",
            "send_mode": "BROWSER" if browser else "GRAPH_API", "send_ready": True,
            "is_active": True, "has_page_token": not browser, "rule_enabled": False,
            "antidetect_profile_id": profile_id, "antidetect_profile_name": profile_id,
            "browser_status": "READY", "token_status": "OK", "source_synced_at": "2026-11-18T01:00:00Z",
            "max_comments_per_day": 20, "comments_used_today": 2, "comments_remaining_today": 18,
        })
    for profile_id in sorted({p["antidetect_profile_id"] for p in pages if p["send_mode"] == "BROWSER"}):
        members = [p for p in pages if p["antidetect_profile_id"] == profile_id]
        blocked = profile_id == "synthetic-0"
        waiting = profile_id == "synthetic-1"
        profiles.append({
            "profile_id": profile_id, "profile_name": profile_id, "page_count": len(members),
            "page_names": [p["page_name"] for p in members],
            "status": "BLOCKED" if blocked else "WAITING" if waiting else "READY",
            "reason": "PROFILE_BLOCKED" if blocked else "WAIT_PROFILE_GAP" if waiting else "READY",
            "wait_reason": "Synthetic checkpoint" if blocked else "Synthetic spacing wait" if waiting else None,
            "operations_today": None if blocked else 7, "operations_limit": None if blocked else 100,
            "scan_operations_limit": None if blocked else 75, "reserved_send_operations": None if blocked else 25,
            "retry_at": "2026-11-18T02:00:00Z" if waiting else None,
            "blocked_page_ids": [p["page_id"] for p in members] if blocked else [],
            "verified_page_ids": [] if blocked else [p["page_id"] for p in members],
        })
    counts = {key: 0 for key in ("PENDING", "SENDING", "SUCCESS", "FAILED", "NEED_CHECK", "CANCELLED", "PAUSED")}
    counts["NEED_CHECK"] = 1
    fixture = {
        "overview": {"metrics": {"paused_all": True, "sheet_sync": {"sheet_url": "https://docs.google.com/spreadsheets/d/synthetic/edit"}}, "task_counts": counts},
        "pages": pages, "tasks": [], "profile_safety": {"profiles": profiles, "paused_all": True, "read_only": True},
    }
    preflight = {"paused_all": True, "read_only": True, "generated_at": "2026-11-18T01:00:00Z",
                 "eligible_pages": 0, "enabled_pages": 0, "blocked_pages": 8, "blocked_profiles": 1,
                 "source_errors": 0, "needs_check_count": 1, "task_counts": counts,
                 "findings": [{"severity": "info", "code": "SYSTEM_PAUSED", "reason": "Synthetic Pause"}]}
    traffic, pending_checks, pending_resumes, errors = [], [], [], []

    def intercept(route):
        from urllib.parse import urlsplit
        path = urlsplit(route.request.url).path
        traffic.append((route.request.method, path))
        if path == "/commenter/":
            route.fulfill(content_type="text/html", body=(WEB / "templates" / "commenter.html").read_text(encoding="utf-8"))
            return
        if path in ("/commenter/actions.js", "/commenter/styles.css"):
            asset = "commenter.js" if path.endswith(".js") else "commenter.css"
            route.fulfill(content_type="application/javascript" if path.endswith(".js") else "text/css",
                          body=(WEB / "static" / asset).read_text(encoding="utf-8"))
            return
        if path == "/api/commenter/state":
            data = copy.deepcopy(fixture)
        elif path == "/api/commenter/preflight":
            data = preflight
        elif path.endswith("/preview"):
            data = {"comment_sequence": [{"delay_minutes": 30, "template": "Synthetic first comment"},
                                          {"delay_minutes": 50, "template": "Synthetic next comment"}]}
        elif path.endswith("/check"):
            pending_checks.append(route)
            return
        elif path == "/api/commenter/system/pause":
            if route.request.post_data_json == {"paused": False}:
                pending_resumes.append(route)
                return
            assert route.request.post_data_json == {"paused": True}
            fixture["overview"]["metrics"]["paused_all"] = True
            data = {"success": True, "paused": True}
        else:
            route.abort()
            raise AssertionError("Unexpected UI request: " + path)
        route.fulfill(content_type="application/json", body=json.dumps(data))

    OUTPUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/*", intercept)
            page.goto("http://commenter.invalid/commenter/", wait_until="networkidle")
            assert page.locator("#stat-pages").inner_text() == "66"
            assert not any(method == "POST" for method, _ in traffic)
            assert not any(path.endswith("preflight") for _, path in traffic)
            page.locator("#tab-safety").click()
            assert page.locator(".profile-safety-card").count() == 12
            assert page.locator(".profile-safety-card.blocked").count() == 1
            assert page.locator(".profile-safety-card.waiting").count() == 1
            assert "— / —" in page.locator(".profile-safety-card.blocked").inner_text()
            page.locator("#run-preflight").click()
            page.wait_for_function("document.getElementById('preflight-findings').children.length > 0")
            assert "toàn bộ" in page.locator("#preflight-summary").inner_text()
            assert not any(method == "POST" for method, _ in traffic)
            page.screenshot(path=str(OUTPUT / "profiles-desktop.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.locator("body").evaluate("element => element.scrollWidth") <= 390
            page.screenshot(path=str(OUTPUT / "profiles-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.locator("#tab-pages").click()
            page.locator("#mode-filter").select_option("BROWSER")
            page.locator("#page-search").fill("Synthetic Page 20")
            page.locator(".page-name").click()
            assert page.get_by_role("button", name="Bật tự động Page", exact=True).is_disabled()
            assert page.get_by_role("button", name="Khôi phục tác vụ tạm dừng", exact=True).is_disabled()
            page.locator("#close-page").click()
            # A check may take seconds; Stop must stay available during that wait.
            fixture["overview"]["metrics"]["paused_all"] = False
            page.locator("#refresh").click()
            page.wait_for_function("document.getElementById('runtime-state').textContent.includes('đang chạy')")
            page.locator("#mode-filter").select_option("GRAPH_API")
            page.locator("#page-search").fill("Synthetic Page 01")
            page.locator(".page-name").click()
            page.get_by_role("button", name="Kiểm tra Graph API", exact=True).click()
            page.wait_for_function("document.querySelector('#page-detail select').disabled")
            page.locator("#close-page").click()
            assert page.locator("#pause-toggle").is_enabled()
            page.locator("#pause-toggle").click()
            page.wait_for_function("document.getElementById('runtime-state').textContent.includes('tạm dừng')")
            assert pending_checks
            for route in pending_checks:
                route.fulfill(content_type="application/json", body='{"success":true}')
            page.wait_for_function("!document.getElementById('pause-toggle').disabled")
            page.locator("#pause-toggle").click()
            page.locator("#confirm-check").check()
            page.locator("#confirm-submit").click()
            page.wait_for_function("document.querySelector('#sheet-submit').disabled")
            page.locator("#pause-toggle").click()
            page.wait_for_function("document.getElementById('runtime-state').textContent.includes('tạm dừng')")
            assert pending_resumes
            for route in pending_resumes:
                route.fulfill(content_type="application/json", body='{"success":true,"paused":true}')
            page.wait_for_function("!document.getElementById('pause-toggle').disabled")
            assert [path for method, path in traffic if method == "POST"] == [
                "/api/commenter/pages/61570000000000/check", "/api/commenter/system/pause",
                "/api/commenter/system/pause", "/api/commenter/system/pause"]
            assert not errors, errors
            print("UI verified: 66 synthetic Pages, profile states, read-only preflight, mobile layout, Pause during pending check and Resume.")
            print("Screenshots: " + str(OUTPUT))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
