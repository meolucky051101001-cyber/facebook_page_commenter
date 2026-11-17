"""Antidetect's Commenter workspace: fixed local proxy routes, no browser secrets.

Registration is separate from the Antidetect launcher so this surface can be tested
without opening profiles or initializing either application's production state.
"""
import asyncio
from pathlib import Path
import re
from urllib.parse import urlencode, urlparse

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
import requests


PAGE_FIELDS = frozenset("""
    page_id page_name fb_profile_id connection_mode token_status is_active
    antidetect_profile_id antidetect_profile_name account_name has_page_token
    send_mode browser_status browser_checked_at browser_error connection_checked_at
    connection_error graph_ready browser_ready send_ready automation_eligible
    rule_enabled source_error source_synced_at delay_minutes max_comments_per_day
    comments_used_today comments_remaining_today send_wait_reason next_send_at
    profile_operations_today profile_operations_limit profile_wait_reason profile_retry_at
    scan_error send_error status_note
""".split())
TASK_FIELDS = frozenset("""
    id page_id page_name post_id post_url source batch_id sequence_step
    sequence_gap_minutes comment_content scheduled_at status comment_id
    error_message retry_count updated_at antidetect_profile_name
""".split())
ACTION_FIELDS = frozenset("""
    success message enabled paused send_mode token_status browser_status is_active
    resumed_count errors inserted updated pages_synced templates_synced
""".split())
PREVIEW_FIELDS = frozenset(("templates", "comment_sequence", "delay_minutes", "source_error", "sheet_url"))
TASK_STATUSES = ("PENDING", "SENDING", "SUCCESS", "FAILED", "NEED_CHECK", "CANCELLED", "PAUSED")
PROFILE_FIELDS = frozenset("""profile_id profile_name page_count page_names status reason operations_today
    operations_limit wait_reason retry_at blocked_page_ids verified_page_ids""".split())
FINDING_FIELDS = frozenset(("severity", "code", "page_id", "page_name", "profile_id", "reason"))
PAGE_ISSUE_FIELDS = frozenset(("page_id", "page_name", "profile_id", "reason"))


def _project(value, fields):
    if not isinstance(value, dict):
        raise ValueError("Invalid Commenter response")
    return {key: value[key] for key in fields if key in value}


def _public_report(value, *, preflight=False):
    if (not isinstance(value, dict) or value.get("read_only") is not True
            or type(value.get("paused_all")) is not bool):
        raise ValueError("Invalid readiness report")
    if not isinstance(value.get("generated_at"), str):
        raise ValueError("Missing report timestamp")
    if preflight:
        result = _project(value, ("read_only", "paused_all", "generated_at", "scope", "eligible_pages", "enabled_pages",
                                  "blocked_pages", "blocked_profiles", "source_errors", "needs_check_count"))
        for key in ("eligible_pages", "enabled_pages", "blocked_pages", "blocked_profiles", "source_errors", "needs_check_count"):
            if type(result.get(key)) is not int or result[key] < 0:
                raise ValueError("Invalid readiness count")
        if not isinstance(value.get("findings"), list):
            raise ValueError("Invalid findings")
        result["task_counts"] = _project(value["task_counts"], TASK_STATUSES)
        if any(type(result["task_counts"].get(key)) is not int or result["task_counts"][key] < 0 for key in TASK_STATUSES):
            raise ValueError("Invalid task counts")
        result["findings"] = [_project(item, FINDING_FIELDS) for item in value["findings"]]
        for item in result["findings"]:
            if (item.get("severity") not in ("info", "warning", "error")
                    or not isinstance(item.get("code"), str) or not isinstance(item.get("reason"), str)):
                raise ValueError("Invalid finding")
            if any(item.get(key) is not None and not isinstance(item[key], str) for key in ("page_id", "page_name", "profile_id")):
                raise ValueError("Invalid finding location")
    else:
        result = _project(value, ("read_only", "paused_all", "generated_at"))
        for key, fields in (("profiles", PROFILE_FIELDS), ("blocked_pages", PAGE_ISSUE_FIELDS), ("pages_needing_attention", PAGE_ISSUE_FIELDS)):
            if not isinstance(value.get(key), list):
                raise ValueError("Invalid profile report")
            result[key] = [_project(item, fields) for item in value[key]]
        for item in result["profiles"]:
            if (item.get("status") not in ("READY", "WAITING", "BLOCKED")
                    or not isinstance(item.get("profile_id"), str) or not item["profile_id"]
                    or type(item.get("page_count")) is not int or item["page_count"] < 1):
                raise ValueError("Invalid profile gate")
            for key in ("page_names", "blocked_page_ids", "verified_page_ids"):
                if not isinstance(item.get(key), list) or any(not isinstance(text, str) for text in item[key]):
                    raise ValueError("Invalid profile members")
            for key in ("operations_today", "operations_limit"):
                if item.get(key) is not None and (type(item[key]) is not int or item[key] < 0):
                    raise ValueError("Invalid profile counter")
            if any(item.get(key) is not None and not isinstance(item[key], str)
                   for key in ("profile_name", "reason", "wait_reason", "retry_at")):
                raise ValueError("Invalid profile details")
        for item in result["blocked_pages"] + result["pages_needing_attention"]:
            if any(item.get(key) is not None and not isinstance(item[key], str) for key in PAGE_ISSUE_FIELDS):
                raise ValueError("Invalid Page issue")
    return result


async def _body(request, required, optional=()):
    try:
        value = await request.json()
    except ValueError:
        value = None
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise HTTPException(400, "Dữ liệu thao tác không hợp lệ.")
    return value


def register_commenter_routes(app, authorized, templates, local_request):
    """Register only explicitly supported operations on the loopback Commenter."""
    assets = Path(__file__).with_name("static")

    def require_session(request):
        if not authorized(request):
            raise HTTPException(401, "Phiên truy cập hết hạn. Hãy mở lại Antidetect.")

    async def proxy(method, path, body=None, fields=ACTION_FIELDS):
        try:
            status, data = await asyncio.to_thread(local_request, method, path, body)
            if not isinstance(status, int) or not 200 <= status <= 599 or not isinstance(data, dict):
                raise ValueError("Invalid Commenter response")
            if status >= 400:
                # Only public endpoint messages are forwarded, never raw responses.
                detail = data.get("detail") or data.get("error")
                return JSONResponse({"detail": detail if isinstance(detail, str) else "Không thực hiện được thao tác."}, status_code=status)
            return JSONResponse(_project(data, fields), status_code=status)
        except (OSError, ValueError, TypeError, requests.RequestException):
            raise HTTPException(502, "Không kết nối được Commenter cục bộ. Kiểm tra dịch vụ rồi làm mới.") from None

    @app.get("/commenter/")
    async def commenter_overview(request: Request):
        if not authorized(request):
            return templates.TemplateResponse(request=request, name="unlock.html", context={}, status_code=401)
        return templates.TemplateResponse(request=request, name="commenter.html", context={})

    @app.get("/commenter/actions.js", include_in_schema=False)
    async def commenter_script():
        return FileResponse(assets / "commenter.js", media_type="application/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/commenter/styles.css", include_in_schema=False)
    async def commenter_styles():
        return FileResponse(assets / "commenter.css", media_type="text/css", headers={"Cache-Control": "no-store"})

    @app.get("/api/commenter/state")
    async def commenter_state(request: Request):
        require_session(request)
        try:
            results = await asyncio.gather(*(
                asyncio.to_thread(local_request, "GET", path)
                for path in ("/api/metrics", "/api/pages", "/api/tasks?limit=200", "/api/profile-safety")
            ))
            if any(status != 200 for status, _ in results):
                raise ValueError("Commenter unavailable")
            overview, catalog, queue, safety = (data for _, data in results)
            metrics = overview["metrics"]
            if not isinstance(metrics, dict) or type(metrics.get("paused_all")) is not bool:
                raise ValueError("Unknown pause state")
            safe_overview = {
                "metrics": {
                    "paused_all": metrics["paused_all"],
                    "sheet_sync": _project(metrics.get("sheet_sync") or {}, (
                        "sheet_url", "last_run", "last_sync_at", "last_success_at", "last_attempt_at", "last_error",
                    )),
                },
                "task_counts": _project(overview["task_counts"], TASK_STATUSES),
                "total_pages": overview.get("total_pages"),
                "active_pages": overview.get("active_pages"),
            }
            return {"overview": safe_overview,
                    "pages": [_project(page, PAGE_FIELDS) for page in catalog["pages"]],
                    "tasks": [_project(task, TASK_FIELDS) for task in queue["tasks"]],
                    "profile_safety": _public_report(safety)}
        except (OSError, ValueError, TypeError, KeyError, requests.RequestException):
            raise HTTPException(502, "Không tải được trạng thái Commenter. Các thao tác chạy đã được khóa trên giao diện.") from None

    @app.get("/api/commenter/preflight")
    async def commenter_preflight(request: Request):
        require_session(request)
        try:
            status, data = await asyncio.to_thread(local_request, "GET", "/api/preflight")
            if status != 200:
                raise ValueError("Preflight unavailable")
            return _public_report(data, preflight=True)
        except (OSError, ValueError, TypeError, KeyError, requests.RequestException):
            raise HTTPException(502, "Chưa hoàn tất preflight. Giữ Pause và thử lại.") from None

    @app.get("/api/commenter/tasks")
    async def commenter_tasks(request: Request, status: str = "", page_id: str = ""):
        require_session(request)
        if (status and status not in TASK_STATUSES) or (page_id and not re.fullmatch(r"[0-9]{5,30}", page_id)):
            raise HTTPException(400, "Bộ lọc tác vụ không hợp lệ.")
        query = {"limit": 200}
        if status:
            query["status"] = status
        if page_id:
            query["page_id"] = page_id
        try:
            result_status, data = await asyncio.to_thread(local_request, "GET", "/api/tasks?" + urlencode(query))
            if result_status != 200:
                raise ValueError("Commenter unavailable")
            return {"tasks": [_project(task, TASK_FIELDS) for task in data["tasks"]]}
        except (OSError, ValueError, TypeError, KeyError, requests.RequestException):
            raise HTTPException(502, "Không tải được danh sách tác vụ.") from None

    @app.api_route("/api/commenter/pages/{page_id}/{action}", methods=["GET", "POST"])
    async def commenter_page_action(request: Request, page_id: str, action: str):
        require_session(request)
        methods = {"preview": "GET", "check": "POST", "automation": "POST", "reactivate": "POST",
                   "send_mode": "POST", "resume_tasks": "POST"}
        if not re.fullmatch(r"[0-9]{5,30}", page_id) or methods.get(action) != request.method:
            raise HTTPException(404, "Thao tác Page không hợp lệ.")
        body = None
        if action == "automation":
            body = await _body(request, ("enabled",))
            if type(body["enabled"]) is not bool:
                raise HTTPException(400, "Cần chọn bật hoặc tạm dừng.")
        elif action == "send_mode":
            body = await _body(request, ("send_mode",))
            if body["send_mode"] not in ("GRAPH_API", "BROWSER"):
                raise HTTPException(400, "Cách gửi không hợp lệ.")
        return await proxy(request.method, f"/api/pages/{page_id}/{action}", body,
                           PREVIEW_FIELDS if action == "preview" else ACTION_FIELDS)

    @app.post("/api/commenter/system/pause")
    async def commenter_pause(request: Request):
        require_session(request)
        body = await _body(request, ("paused",))
        if type(body["paused"]) is not bool:
            raise HTTPException(400, "Trạng thái tạm dừng không hợp lệ.")
        return await proxy("POST", "/api/system/pause", body)

    @app.post("/api/commenter/sheets/sync")
    async def commenter_sync_sheet(request: Request):
        require_session(request)
        body = await _body(request, ("sheet_url",))
        raw = body["sheet_url"]
        url = raw.strip() if isinstance(raw, str) else ""
        try:
            parsed = urlparse(url)
        except ValueError:
            raise HTTPException(400, "Hãy nhập đường dẫn Google Sheet hợp lệ.") from None
        if (len(url) > 2048 or parsed.scheme != "https" or parsed.netloc != "docs.google.com"
                or not re.fullmatch(r"/spreadsheets/d/[A-Za-z0-9_-]+(?:/.*)?", parsed.path)):
            raise HTTPException(400, "Hãy nhập đường dẫn Google Sheet hợp lệ.")
        return await proxy("POST", "/api/sheets/sync", {"sheet_url": url})

    @app.post("/api/commenter/tasks/{task_id}/{action}")
    async def commenter_task_action(request: Request, task_id: str, action: str):
        require_session(request)
        if not re.fullmatch(r"[1-9][0-9]{0,18}", task_id) or action not in ("cancel", "retry", "resolve"):
            raise HTTPException(404, "Thao tác tác vụ không hợp lệ.")
        body = None
        if action == "retry":
            body = await _body(request, ("confirmed_not_posted",))
            if type(body["confirmed_not_posted"]) is not bool:
                raise HTTPException(400, "Cần xác nhận kết quả đối chiếu tác vụ.")
        elif action == "resolve":
            body = await _body(request, (), ("comment_id",))
            comment_id = body.get("comment_id")
            if comment_id is not None and (not isinstance(comment_id, str)
                    or not re.fullmatch(r"[0-9]{1,40}(?:_[0-9]{1,40})?", comment_id)):
                raise HTTPException(400, "ID bình luận không hợp lệ.")
        return await proxy("POST", f"/api/tasks/{task_id}/{action}", body)
