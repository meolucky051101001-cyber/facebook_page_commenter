import asyncio
import csv
import hmac
import io
import json
import secrets
import threading
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any, Literal
from urllib.parse import urlencode, urlsplit
from pydantic import BaseModel, Field
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

import config
import database as db
import meta_client
import antidetect_client
import scheduler
import sheets_sync
import spintax
import operations
import comment_dispatch
import service_lock



@asynccontextmanager
async def lifespan(app: FastAPI):
    with service_lock.exclusive_database(db.DB_PATH):
        db.init_db()
        with closing(db.get_connection()) as conn:
            operations.recover_interrupted_operations(conn)
        scheduler.SCHEDULER_METRICS["paused_all"] = db.get_setting("automation_run_state") != "running"
        loops = [asyncio.create_task(loop()) for loop in (
            scheduler.loop_post_scanner, scheduler.loop_task_executor, scheduler.loop_maintenance,
            operations.loop_publications, operations.loop_inbox,
        )]
        try:
            yield
        finally:
            scheduler.SCHEDULER_METRICS["paused_all"] = True
            for task in loops:
                task.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            await asyncio.to_thread(_drain_workers)


def _drain_workers():
    # to_thread work keeps running after cancellation of its async waiter.
    for lock in (scheduler._SCAN_LOCK, scheduler._EXEC_LOCK,
                 scheduler._COMMENT_SHEET_LOCK, operations._publication_lock, operations._inbox_lock):
        with lock:
            pass
    comment_dispatch.wait_until_idle()


app = FastAPI(title="Facebook Page Multi-Account Commenter Hub", lifespan=lifespan)


@app.middleware("http")
async def authorize_dashboard(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        if origin is not None and origin != f"{request.url.scheme}://{host}":
            return JSONResponse({"detail": "Origin không hợp lệ."}, status_code=403)
        secret = config.DASHBOARD_ADMIN_KEY
        supplied = request.headers.get("x-dashboard-key", "")
        if len(secret) < 32 or not hmac.compare_digest(secret.encode(), supplied.encode()):
            return JSONResponse({"detail": "Cần khóa quản trị Dashboard hợp lệ."}, status_code=401)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    return response


app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"], www_redirect=False)


# --- Pydantic Request Models ---
class ConnectAccountRequest(BaseModel):
    account_name: str
    user_access_token: str


class FacebookLoginRequest(BaseModel):
    account_name: str = Field(default="", max_length=120)
    publish_access: bool = False
    inbox_access: bool = False


class SyncSheetRequest(BaseModel):
    sheet_url: str


class UpdateRuleRequest(BaseModel):
    max_comments_per_day: int = 20
    is_enabled: Optional[bool] = None


class ManualTaskRequest(BaseModel):
    page_id: str
    post_id_or_url: str
    content: str
    post_now: bool = True
    scheduled_at: Optional[str] = None
    batch_id: Optional[str] = None


class SpintaxPreviewRequest(BaseModel):
    content: str


class PauseRequest(BaseModel):
    paused: bool


class PageAutomationRequest(BaseModel):
    enabled: bool


class RetryTaskRequest(BaseModel):
    confirmed_not_posted: bool = False


class ResolveTaskRequest(BaseModel):
    comment_id: Optional[str] = None


class PublicationRequest(BaseModel):
    page_id: str = Field(min_length=1, max_length=32)
    external_key: str = Field(min_length=1, max_length=200)
    media_type: Literal["text", "photo", "reel"] = "text"
    media_url: str = Field(default="", max_length=4096)
    caption: str = Field(max_length=8000)
    first_comment: str = Field(default="", max_length=8000)
    comment_delay_minutes: int = Field(default=0, ge=0, le=1440)
    scheduled_at: datetime


class PublicationBatch(BaseModel):
    entries: List[PublicationRequest] = Field(min_length=1, max_length=100)


class PublicationCSV(BaseModel):
    content: str = Field(min_length=1, max_length=500_000)


class ModerationRequest(BaseModel):
    action: Literal["reply", "hide", "unhide"]
    message: str = Field(default="", max_length=8000)


class DraftRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class ModerationRulesRequest(BaseModel):
    hide_phone: bool = False
    keyword: str = Field(default="", max_length=100)
    reply: str = Field(default="", max_length=8000)


@app.get("/api/publications")
def publication_list():
    return {"publications": operations.list_publications()}


@app.post("/api/publications")
def publication_add(req: PublicationBatch):
    try:
        return {"inserted": operations.add_publications(req.entries)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/publications/import")
def publication_import(req: PublicationCSV):
    try:
        reader = csv.DictReader(io.StringIO(req.content.lstrip("\ufeff")), strict=True)
        entries = []
        for row in reader:
            if not any(row.values()):
                continue
            if len(entries) >= 100:
                raise ValueError("Mỗi file nhập tối đa 100 bài.")
            entry = {key: value for key, value in row.items() if value not in (None, "")}
            entries.append(PublicationRequest.model_validate(entry))
        if not entries:
            raise ValueError("File không có lịch đăng.")
        return {"inserted": operations.add_publications(entries)}
    except (ValueError, csv.Error):
        raise HTTPException(400, "Kiểm tra cột CSV, ID Page, mã lịch duy nhất và giờ đăng có múi giờ.") from None


@app.post("/api/publications/{publication_id}/cancel")
def publication_cancel(publication_id: int):
    if not operations.cancel_publication(publication_id):
        raise HTTPException(409, "Chỉ hủy được bài đang chờ.")
    return {"success": True}


@app.get("/api/inbox")
def inbox_list(page_id: Optional[str] = None):
    pages = db.get_all_pages()
    return {"comments": operations.list_inbox(page_id), "monitor_enabled": db.get_setting("inbox_monitor") == "enabled",
            "page_waits": {page["page_id"]: comment_dispatch.page_status(page, scheduler.SCHEDULER_METRICS["paused_all"])
                           for page in pages if page_id is None or page["page_id"] == page_id}}


@app.post("/api/inbox/monitor")
def inbox_monitor(req: PageAutomationRequest):
    db.set_setting("inbox_monitor", "enabled" if req.enabled else "disabled")
    return {"enabled": req.enabled}


@app.get("/api/inbox/rules/{page_id}")
def inbox_rules(page_id: str):
    return operations.get_moderation_rules(page_id)


@app.post("/api/inbox/rules/{page_id}")
def inbox_rules_update(page_id: str, req: ModerationRulesRequest):
    try:
        return operations.set_moderation_rules(page_id, req.hide_phone, req.keyword, req.reply)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/inbox/refresh/{page_id}")
def inbox_refresh(page_id: str):
    try:
        return {"read": operations.refresh_inbox(page_id)}
    except (ValueError, meta_client.MetaAPIError) as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(502, "Không đọc được bình luận từ Meta.") from None


@app.post("/api/inbox/{comment_id}/moderate")
def inbox_moderate(comment_id: str, req: ModerationRequest):
    try:
        result = operations.moderate(comment_id, req.action, req.message)
        if result.get("waiting"):
            return JSONResponse({**result, "detail": result["message"]}, status_code=429 if result.get("retry_at") else 409)
        return result
    except (ValueError, meta_client.MetaAPIError) as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/api/reports")
def report(days: int = Query(7, ge=1, le=31)):
    return {"rows": operations.get_report(days), "scan_errors": scheduler.SCHEDULER_METRICS["scan_errors"],
            "send_errors": scheduler.SCHEDULER_METRICS["send_errors"],
            "inbox_errors": scheduler.SCHEDULER_METRICS.get("inbox_errors", {}),
            "publication_error": scheduler.SCHEDULER_METRICS.get("publication_error")}


@app.get("/operations", response_class=HTMLResponse)
def operations_dashboard():
    return HTMLResponse(Path(__file__).with_name("operations.html").read_text(encoding="utf-8"))


@app.post("/api/drafts/rewrite")
def draft_rewrite(req: DraftRequest):
    try:
        return {"text": operations.rewrite_draft(req.text)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(502, "Không lấy được bản nháp từ Gemini.") from None


# --- REST API Endpoints ---

@app.get("/api/metrics")
def get_metrics():
    tasks = db.get_all_tasks(limit=500)
    pages = db.get_all_pages()
    counts = {
        "PENDING": 0, "SENDING": 0, "SUCCESS": 0,
        "FAILED": 0, "NEED_CHECK": 0, "CANCELLED": 0, "PAUSED": 0
    }
    for t in tasks:
        st = t.get("status")
        if st in counts:
            counts[st] += 1

    return {
        "metrics": scheduler.SCHEDULER_METRICS,
        "task_counts": counts,
        "total_pages": len(pages),
        "active_pages": sum(1 for p in pages if p.get("is_active") and p.get("rule_enabled") and not p.get("source_error")),
        "has_app_credentials": config.has_app_credentials(),
        "has_facebook_login_config": bool(config.has_app_credentials() and config.META_OAUTH_REDIRECT_URI),
    }


@app.post("/api/system/pause")
def set_pause(req: PauseRequest):
    if not req.paused:
        enabled_pages = [p for p in db.get_all_pages() if p.get("rule_enabled")]
        if enabled_pages:
            result = scheduler.refresh_comment_sheet(force=True)
            if not result.get("success"):
                raise HTTPException(status_code=409, detail=result.get("error", "Không đồng bộ được tab bình luận."))
    scheduler.set_system_paused(req.paused)
    return {"success": True, "paused": req.paused}


@app.post("/api/sync")
def trigger_manual_sync(background_tasks: BackgroundTasks):
    if scheduler.SCHEDULER_METRICS["paused_all"]:
        raise HTTPException(
            status_code=409,
            detail="Hệ thống đang tạm dừng. Hãy bấm 'Tiếp tục chạy' trước khi quét và gửi bình luận."
        )
    background_tasks.add_task(scheduler.run_manual_sync_cycle)
    return {"success": True, "message": "Đã bắt đầu quét bài mới và thực thi các tác vụ đến hạn."}


@app.get("/api/antidetect/status")
def get_antidetect_status():
    return antidetect_client.list_antidetect_profiles()


@app.post("/api/antidetect/sync")
def sync_from_antidetect():
    res = antidetect_client.sync_pages_from_antidetect(export_csv=True)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Không thể đồng bộ từ Antidetect Python"))
    msg = (
        f"Đã đồng bộ {res['profiles_synced']} Profile từ Antidetect Python: "
        f"thêm mới {res['inserted']} Page, ghép Hybrid {res['linked_hybrid']} Page, "
        f"cập nhật {res['updated']} Page (Tổng: {res['total_pages_in_db']} Fanpage)."
    )
    if res.get("conflicting_page_ids"):
        msg += f" Cần kiểm tra {len(res['conflicting_page_ids'])} ID trùng."
    res["message"] = msg
    return res


@app.get("/api/pages")
def list_pages():
    pages = db.get_all_pages()
    for page in pages:
        page.update(comment_dispatch.page_status(page, scheduler.SCHEDULER_METRICS["paused_all"]))
        page["graph_ready"] = bool(
            page.get("is_active") and page.get("has_page_token")
            and page.get("token_status") in ("OK", "EXPIRING_SOON")
            and (page["page_id"] == config.PILOT_PAGE_ID
                 or (page.get("connection_checked_at") and not page.get("connection_error")))
        )
        page["automation_eligible"] = page["graph_ready"] and not page.get("source_error")
        page["scan_error"] = scheduler.SCHEDULER_METRICS["scan_errors"].get(page["page_id"])
        page["send_error"] = scheduler.SCHEDULER_METRICS["send_errors"].get(page["page_id"])
    return {"pages": pages}


@app.get("/api/pages/{page_id}/recent_posts")
def get_page_posts(page_id: str):
    page = db.get_page_with_decrypted_token(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Không tìm thấy Page")
    if not scheduler.graph_ready(page):
        raise HTTPException(status_code=409, detail="Page chưa có kết nối Graph API khả dụng.")
    try:
        posts = meta_client.get_recent_posts(page_id, page["page_access_token"], limit=15)
        return {"posts": posts, "source": "GRAPH_API"}
    except meta_client.MetaAPIError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from None
    except Exception:
        raise HTTPException(status_code=502, detail="Không tải được bài từ Meta.") from None


def verify_connected_page(page_id: str):
    page = db.get_page_with_decrypted_token(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Không tìm thấy Page")
    if not page.get("page_access_token"):
        raise HTTPException(status_code=409, detail="Page chưa có token Graph API khả dụng.")
    result = meta_client.verify_page_connection(page_id, page["page_access_token"])
    info = result.get("token_info") or {}
    status = result.get("failure_status")
    if result.get("success"):
        status = info.get("status") or "OK"
    error = result.get("error")
    if error:
        error = error.replace(page["page_access_token"], "[đã ẩn]")
        if config.META_APP_SECRET:
            error = error.replace(config.META_APP_SECRET, "[đã ẩn]")
    db.update_page_connection_check(
        page_id, datetime.now(timezone.utc).isoformat(), error, status,
        info.get("missing_scopes"), info.get("expires_at"),
    )
    if not result.get("success"):
        raise HTTPException(status_code=409, detail=error or "Kiểm tra kết nối Page thất bại.")
    return status


@app.post("/api/pages/{page_id}/check")
def check_page_connection(page_id: str):
    return {"success": True, "token_status": verify_connected_page(page_id),
            "message": "Đã xác minh đúng Page, quyền bình luận và quyền đọc bài."}


@app.get("/api/pages/{page_id}/preview")
def preview_page(page_id: str):
    page = db.get_page(page_id)
    if not page or page["page_id"] != page_id:
        raise HTTPException(status_code=404, detail="Không tìm thấy Page")
    return {
        "templates": json.loads(page.get("templates") or "[]"),
        "comment_sequence": json.loads(page.get("comment_sequence") or "[]"),
        "delay_minutes": page.get("delay_minutes"),
        "source_error": page.get("source_error"),
        "sheet_url": scheduler.comment_sheet_url(),
    }


@app.post("/api/pages/{page_id}/automation")
def set_page_automation(page_id: str, req: PageAutomationRequest):
    if req.enabled:
        verify_connected_page(page_id)
        page = db.get_page(page_id)
        if not page["is_active"]:
            raise HTTPException(status_code=409, detail="Hãy kích hoạt lại kết nối Meta của Page trước.")
        result = scheduler.refresh_comment_sheet(force=True)
        if not result.get("success"):
            raise HTTPException(status_code=409, detail=result.get("error") or "Không đọc được tab bình luận.")
        page = db.get_page(page_id)
        if page.get("source_error") or not page.get("source_synced_at"):
            raise HTTPException(status_code=409, detail=page.get("source_error") or "Page chưa có mẫu trong Sheet.")
    result = db.set_page_automation(page_id, req.enabled)
    if not result["success"]:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/pages/{page_id}/resume_tasks")
def resume_page_tasks(page_id: str):
    page = db.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Không tìm thấy Page")
    if not scheduler.graph_ready(db.get_page_with_decrypted_token(page_id) or {}):
        raise HTTPException(status_code=409, detail="Page chưa sẵn sàng qua Graph API.")
    try:
        resumed = scheduler.resume_tasks(page_id=page["page_id"])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"success": True, "resumed_count": resumed, "message": f"Đã khôi phục {resumed} tác vụ tạm dừng về hàng đợi."}


@app.post("/api/pages/{page_id}/reactivate")
def reactivate_page(page_id: str):
    status = verify_connected_page(page_id)
    db.update_page_status(page_id, is_active=True, token_status=status)
    return {"success": True, "message": "Đã kích hoạt lại Page.", "is_active": True}


@app.post("/api/accounts/connect")
def connect_account(req: ConnectAccountRequest):
    """
    Connects a Facebook Account using User Access Token.
    Exchanges short token for long-lived token, checks scopes, and saves pages securely.
    """
    return _connect_account(req.account_name, req.user_access_token)


def _connect_account(account_name: str, user_access_token: str, require_user: bool = False):
    token = user_access_token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Vui lòng nhập Access Token")

    warnings: List[str] = []

    # 1. Exchange for long-lived token
    if not config.has_app_credentials():
        long_lived_token = token
        warnings.append(
            "Chưa cấu hình META_APP_ID và META_APP_SECRET trong file .env — "
            "Token chưa được đổi sang dài hạn, có thể hết hạn sau 1–2 giờ."
        )
    else:
        try:
            long_lived_token, _ = meta_client.exchange_for_long_lived_user_token(token)
        except meta_client.MetaAPIError as e:
            raise HTTPException(status_code=400, detail=f"Không thể đổi sang Token dài hạn: {e.message}")
        except Exception as e:
            raise HTTPException(status_code=500, detail="Không thể đổi token dài hạn.")

    # 2. Get User Info
    try:
        user_info = meta_client.get_user_info(long_lived_token)
        user_id = str(user_info.get("id")) if user_info.get("id") else None
    except Exception:
        user_info = {}
        user_id = None
    if require_user and not user_id:
        raise HTTPException(400, "Không xác nhận được tài khoản Facebook.")
    account_name = account_name.strip() or user_info.get("name") or "Facebook"

    # 3. Get Pages
    try:
        pages = meta_client.get_user_pages(long_lived_token)
    except meta_client.MetaAPIError as e:
        raise HTTPException(status_code=400, detail=e.message)
    except Exception as e:
        raise HTTPException(status_code=500, detail="Không thể kết nối Meta API.")

    if not pages:
        return {
            "success": True,
            "message": "Không tìm thấy Page nào do tài khoản này quản lý.",
            "pages_added": 0,
            "warnings": warnings
        }

    # Upsert account record
    acc_id = db.upsert_account(
        account_name=account_name,
        user_id=user_id,
        long_lived_user_token=long_lived_token
    )

    added = 0
    for p in pages:
        page_id = str(p.get("id") or "")
        page_name = p.get("name") or page_id
        page_token = p.get("access_token")
        if page_id.isascii() and page_id.isdecimal() and page_token:
            info = meta_client.debug_token(page_token)
            if info.get("checked") is True:
                t_status = info.get("status", "OK")
                missing_scopes = info.get("missing_scopes", [])
                expires_at = info.get("expires_at")
            else:
                t_status = "UNVERIFIED"
                missing_scopes = []
                expires_at = None
                warnings.append(f"Page '{page_name}': chưa xác minh được token; kiểm tra kết nối trước khi bật tự động.")

            status_note = f"Thiếu quyền: {', '.join(missing_scopes)}" if missing_scopes else None
            db.upsert_page(
                page_id=page_id,
                page_name=page_name,
                page_access_token=page_token,
                account_id=acc_id,
                token_status=t_status,
                missing_permissions=missing_scopes,
                expires_at=expires_at,
                status_note=status_note
            )
            if missing_scopes:
                warnings.append(f"Page '{page_name}' thiếu quyền: {', '.join(missing_scopes)}")
            added += 1

    msg = f"Đã kết nối thành công tài khoản '{account_name}' và nạp {added} Fanpage."
    return {"success": True, "message": msg, "pages_added": added, "warnings": warnings}


_facebook_login_lock = threading.Lock()
_facebook_logins: dict[str, tuple[float, str]] = {}
_facebook_login_cookie = "commenter_facebook_state"
_facebook_callback = "/facebook/callback"


def _facebook_redirect_uri(request: Request) -> str:
    raw = config.META_OAUTH_REDIRECT_URI
    if not config.has_app_credentials() or not raw:
        raise HTTPException(409, "Cần cấu hình Meta App và META_OAUTH_REDIRECT_URI.")
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1"}
        or parsed.path != _facebook_callback
        or parsed.query or parsed.fragment or parsed.username or parsed.password
        or raw != str(request.url_for("facebook_login_callback"))
    ):
        raise HTTPException(409, "Mở dashboard tại đúng địa chỉ localhost đã đăng ký làm callback trong Meta App.")
    return raw


@app.post("/api/accounts/facebook/start")
def facebook_login_start(request: Request, body: FacebookLoginRequest):
    redirect_uri = _facebook_redirect_uri(request)
    state = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _facebook_login_lock:
        for key, (expires, _) in list(_facebook_logins.items()):
            if expires <= now:
                del _facebook_logins[key]
        previous = request.cookies.get(_facebook_login_cookie)
        if previous:
            _facebook_logins.pop(previous, None)
        if len(_facebook_logins) >= 32:
            raise HTTPException(429, "Có quá nhiều phiên đăng nhập đang chờ.")
        _facebook_logins[state] = (now + 600, body.account_name)

    scopes = set(meta_client.REQUIRED_SCOPES)
    if body.publish_access:
        scopes.add("pages_manage_posts")
    if body.inbox_access:
        scopes.add("pages_read_user_content")
    query = urlencode({
        "client_id": config.META_APP_ID, "redirect_uri": redirect_uri, "state": state,
        "response_type": "code", "scope": ",".join(sorted(scopes)),
    })
    response = JSONResponse({"url": f"https://www.facebook.com/{config.GRAPH_API_VERSION}/dialog/oauth?{query}"})
    response.set_cookie(
        _facebook_login_cookie, state, max_age=600, httponly=True,
        secure=request.url.scheme == "https", samesite="lax",
    )
    return response


@app.get(_facebook_callback, name="facebook_login_callback")
def facebook_login_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    def done(status: str, count: int = 0, clear_cookie: bool = True):
        response = RedirectResponse("/?" + urlencode({"facebook_login": status, "pages": count}), status_code=303)
        if clear_cookie:
            response.delete_cookie(_facebook_login_cookie)
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    cookie = request.cookies.get(_facebook_login_cookie, "")
    if not state or not state.isascii() or not cookie.isascii() or not hmac.compare_digest(state, cookie):
        return done("invalid", clear_cookie=False)
    with _facebook_login_lock:
        login = _facebook_logins.pop(state, None)
    if not login or login[0] <= time.monotonic():
        return done("invalid")
    if error or not code:
        return done("cancelled")
    try:
        redirect_uri = _facebook_redirect_uri(request)
        token = meta_client.exchange_facebook_login_code(code, redirect_uri)
        result = _connect_account(login[1], token, require_user=True)
    except Exception:
        return done("failed")
    status = "check" if result["warnings"] else "connected"
    return done(status, result["pages_added"])


@app.post("/api/sheets/sync")
def sync_google_sheet(req: SyncSheetRequest):
    url = req.sheet_url.strip() or scheduler.comment_sheet_url()
    result = scheduler.refresh_comment_sheet(force=True, candidate_url=url)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Lỗi đồng bộ Google Sheet"))
    return result


@app.post("/api/pages/{page_id}/rule")
def update_rule(page_id: str, req: UpdateRuleRequest):
    page = db.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Không tìm thấy Page")
    if page["page_id"] != page_id:
        raise HTTPException(status_code=404, detail="Không tìm thấy Page")
    if not 1 <= req.max_comments_per_day <= 200:
        raise HTTPException(status_code=400, detail="Giới hạn mỗi ngày phải từ 1 đến 200.")
    if req.is_enabled is not None and req.is_enabled != bool(page["rule_enabled"]):
        raise HTTPException(status_code=409, detail="Dùng nút bật hoặc tạm dừng trong danh sách Page.")
    db.update_page_daily_limit(page_id, req.max_comments_per_day)
    return {"success": True, "message": "Đã lưu giới hạn bình luận mỗi ngày."}


@app.post("/api/spintax/preview")
def preview_spintax(req: SpintaxPreviewRequest):
    raw = req.content.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Vui lòng nhập nội dung cần xem trước")
    if not sheets_sync.validate_spintax_braces(raw):
        raise HTTPException(status_code=400, detail="Cú pháp Spintax bị lỗi đóng/mở ngoặc nhọn '{...}'")
    return {"success": True, "preview": spintax.spin(raw)}


@app.post("/api/tasks/manual")
def create_manual_task(req: ManualTaskRequest):
    page = db.get_page_with_decrypted_token(req.page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Không tìm thấy Page")
    if not scheduler.graph_ready(page):
        raise HTTPException(status_code=409, detail="Page chưa có kết nối Graph API khả dụng.")
    token = page["page_access_token"]

    raw_content = req.content.strip()
    if not raw_content:
        raise HTTPException(status_code=400, detail="Nội dung bình luận không được để trống")
    if not sheets_sync.validate_spintax_braces(raw_content):
        raise HTTPException(status_code=400, detail="Nội dung bình luận bị lỗi đóng/mở ngoặc nhọn Spintax '{...}'")

    try:
        post_data = meta_client.verify_post_belongs_to_page(req.post_id_or_url, req.page_id, token)
        verified_post_id = post_data["clean_post_id"]
    except meta_client.MetaAPIError as exc:
        raise HTTPException(status_code=400, detail=f"Xác minh bài viết thất bại: {exc.message}") from None
    except Exception:
        raise HTTPException(status_code=502, detail="Không xác minh được bài trên Meta.") from None

    # Store exactly the text confirmed in the dashboard.
    final_content = raw_content
    post_url = post_data.get("permalink_url")

    # 3. Calculate target time
    if req.post_now:
        sched_time = datetime.now(timezone.utc).isoformat()
    else:
        if not req.scheduled_at:
            raise HTTPException(status_code=400, detail="Vui lòng chọn thời gian hẹn giờ")
        try:
            parsed = datetime.fromisoformat(req.scheduled_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed <= datetime.now(timezone.utc):
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=400, detail="Chọn giờ tương lai có múi giờ hợp lệ.") from None
        sched_time = parsed.astimezone(timezone.utc).isoformat()

    batch_id = (req.batch_id or "").strip() or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    success = db.create_task(
        page_id=req.page_id,
        post_id=verified_post_id,
        comment_content=final_content,
        scheduled_at=sched_time,
        source="MANUAL",
        batch_id=batch_id,
        post_url=post_url
    )

    if not success:
        raise HTTPException(status_code=400, detail="Tác vụ này đã tồn tại trong hàng đợi (chống gửi trùng).")

    return {"success": True, "message": "Đã tạo tác vụ bình luận thành công!", "content_preview": final_content}


@app.get("/api/tasks")
def list_tasks(
    page_id: Optional[str] = None,
    status: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 50
):
    tasks = db.get_all_tasks(page_id=page_id, status=status, source=source, limit=limit)
    return {"tasks": tasks}


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: int):
    success = db.cancel_task(task_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Không thể hủy tác vụ (chỉ hủy được khi trạng thái là Chờ lịch hoặc Tạm dừng)."
        )
    return {"success": True, "message": "Đã hủy tác vụ thành công"}


@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: int, req: Optional[RetryTaskRequest] = None):
    try:
        success = scheduler.resume_tasks(task_id=task_id, confirmed_not_posted=bool(req and req.confirmed_not_posted))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Không thể gửi lại. Tác vụ Cần kiểm tra phải được đối chiếu và xác nhận chưa đăng."
        )
    return {"success": True, "message": "Đã khôi phục tác vụ. Lịch gửi và các điều kiện chờ vẫn được áp dụng."}


@app.post("/api/tasks/{task_id}/resolve")
def resolve_task(task_id: int, req: ResolveTaskRequest):
    with closing(db.get_connection()) as conn:
        task = conn.execute("SELECT page_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        raise HTTPException(404, "Không tìm thấy tác vụ.")
    with comment_dispatch.page_lock(task["page_id"]):
        success = db.resolve_task(task_id=task_id, comment_id=req.comment_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Không thể đánh dấu tác vụ này (chỉ áp dụng cho tác vụ Cần kiểm tra, Lỗi hoặc Tạm dừng)."
        )
    return {"success": True, "message": "Đã đánh dấu tác vụ thành công"}


# --- Embedded Web Dashboard UI ---
@app.get("/", response_class=HTMLResponse)
def index():
    html_content = """<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hệ thống Quản lý & Tự động Bình luận Facebook Page</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css" rel="stylesheet">
    <style>
        body { background-color: #f0f2f5; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        .card { border: none; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.06); margin-bottom: 20px; }
        .nav-pills .nav-link { border-radius: 8px; font-weight: 500; color: #495057; }
        .nav-pills .nav-link.active { background-color: #1877f2; color: #fff; }
        .badge-status { font-size: 0.85rem; padding: 6px 12px; border-radius: 20px; }
        .badge-PENDING { background-color: #e3f2fd; color: #0d47a1; }
        .badge-SENDING { background-color: #fff9c4; color: #f57f17; }
        .badge-SUCCESS { background-color: #e8f5e9; color: #1b5e20; }
        .badge-FAILED { background-color: #ffebee; color: #b71c1c; }
        .badge-NEED_CHECK { background-color: #fce4ec; color: #880e4f; font-weight: bold; border: 1px solid #d81b60; }
        .badge-CANCELLED { background-color: #eceff1; color: #37474f; }
        .badge-PAUSED { background-color: #efebe9; color: #4e342e; }
        .navbar-brand { font-weight: 700; color: #1877f2 !important; }
        .post-card { cursor: pointer; transition: 0.2s; border: 1px solid #e0e0e0; border-radius: 8px; padding: 10px; margin-bottom: 8px; }
        .post-card:hover, .post-card.selected { border-color: #1877f2; background-color: #e7f3ff; }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-light bg-white border-bottom sticky-top py-2">
        <div class="container">
            <a class="navbar-brand" href="#"><i class="fa-brands fa-facebook"></i> Page Auto Commenter Hub</a>
            <div class="d-flex align-items-center gap-2">
                <button class="btn btn-sm btn-outline-primary" onclick="triggerManualSync()"><i class="fa-solid fa-rotate"></i> Quét & Chạy ngay</button>
                <span id="systemStatusBadge" class="badge bg-secondary py-2 px-3">Đang kiểm tra trạng thái</span>
                <span id="pilotStatus" class="small text-light"></span>
                <button id="unlockBtn" class="btn btn-sm btn-outline-light" onclick="unlockDashboard()" hidden>Mở khóa Dashboard</button>
                <button id="pauseBtn" class="btn btn-sm btn-outline-warning" onclick="togglePause()"><i class="fa-solid fa-pause"></i> Tạm dừng khẩn cấp</button>
            </div>
        </div>
    </nav>

    <div class="container my-3">
        <!-- App Credentials Warning Banner -->
        <div id="appCredWarning" class="alert alert-warning py-2 mb-3" style="display: none;">
            <i class="fa-solid fa-triangle-exclamation"></i>
            Để dùng nút Kết nối Facebook, cấu hình <code>META_APP_ID</code>, <code>META_APP_SECRET</code>
            và <code>META_OAUTH_REDIRECT_URI</code> trong <code>.env</code>.
        </div>

        <!-- Live Metrics Bar -->
        <div class="row g-2 mb-3">
            <div class="col-md-3">
                <div class="card p-3 text-center mb-0">
                    <small class="text-muted fw-bold">TỔNG FANPAGE</small>
                    <h3 class="fw-bold mb-0 text-primary" id="mTotalPages">0</h3>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card p-3 text-center mb-0">
                    <small class="text-muted fw-bold">CHỜ LỊCH (PENDING)</small>
                    <h3 class="fw-bold mb-0 text-info" id="mPending">0</h3>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card p-3 text-center mb-0">
                    <small class="text-muted fw-bold">ĐÃ ĐĂNG THÀNH CÔNG</small>
                    <h3 class="fw-bold mb-0 text-success" id="mSuccess">0</h3>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card p-3 text-center mb-0">
                    <small class="text-muted fw-bold">CẦN KIỂM TRA (CHỐNG TRÙNG)</small>
                    <h3 class="fw-bold mb-0 text-danger" id="mNeedCheck">0</h3>
                </div>
            </div>
        </div>

        <ul class="nav nav-pills mb-3 bg-white p-2 rounded-3 shadow-sm" id="mainTab" role="tablist">
            <li class="nav-item"><button class="nav-link active" data-bs-toggle="pill" data-bs-target="#tab-sheets"><i class="fa-solid fa-table text-success me-1"></i> 1. Đồng bộ Google Sheet</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-pages"><i class="fa-solid fa-flag me-1"></i> 2. Quản lý Page & Nick</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-rules"><i class="fa-solid fa-sliders me-1"></i> 3. Kịch bản từng Page</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-manual"><i class="fa-solid fa-paper-plane me-1"></i> 4. Hẹn giờ thủ công</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-tasks"><i class="fa-solid fa-clock-rotate-left me-1"></i> 5. Lịch sử tác vụ (<span id="taskCount">0</span>)</button></li>
            <li class="nav-item"><a class="nav-link" href="/operations">Hộp thư, xuất bản &amp; báo cáo</a></li>
        </ul>

        <div class="tab-content">
            <!-- TAB 1: GOOGLE SHEETS SYNC -->
            <div class="tab-pane fade show active" id="tab-sheets">
                <div class="card p-4">
                    <h5 class="fw-bold text-success mb-2"><i class="fa-solid fa-file-excel"></i> Quản lý Cấu hình & Mẫu Comment qua Google Sheet</h5>
                    <p class="text-muted small">
                        Dán URL có gid của tab <strong>Comments - Multi-Page</strong>. Sheet cập nhật mẫu và thời gian chờ cho từng Page.
                    </p>
                    <div class="input-group mb-3">
                        <span class="input-group-text bg-white"><i class="fa-solid fa-link text-muted"></i></span>
                        <input type="text" id="sheetUrlInput" class="form-control" value="" placeholder="Dán link Google Sheet (ví dụ: https://docs.google.com/spreadsheets/d/.../edit)">
                        <button class="btn btn-success px-4" id="btnSyncSheet" onclick="syncGoogleSheet()"><i class="fa-solid fa-arrows-rotate"></i> Đồng bộ ngay</button>
                    </div>


                    <p id="pilotSheetStatus" class="text-muted small"></p>
                    <div id="syncReportBox" style="display:none;" class="mb-3"></div>

                    <div class="alert alert-info py-3 mb-0">
                        <h6 class="fw-bold mb-2">ID trang | Tên Fanpage | Comment | Số phút chờ sau khi đăng | Thứ tự (tùy chọn)</h6>
                        <p class="small mb-0">Định dạng ID trang là văn bản. Để hẹn một chuỗi, đánh số 1, 2, 3 và nhập số phút tăng dần, ví dụ 30, 35, 40. Nếu để trống toàn bộ ô Thứ tự của Page, dùng chung số phút và tool chọn một mẫu cho mỗi bài. Bật hoặc tạm dừng tại danh sách Page.</p>
                    </div>
                </div>
            </div>

            <!-- TAB 2: PAGES & ACCOUNTS -->
            <div class="tab-pane fade" id="tab-pages">
                <div class="row">
                    <div class="col-md-5">
                        <!-- Antidetect Python Direct Sync Card -->
                        <div class="card p-4 border-start border-4 border-success mb-3">
                            <h5 class="fw-bold mb-2 text-success"><i class="fa-solid fa-shield-halved"></i> Đồng bộ từ Antidetect Python</h5>
                            <p class="text-muted small mb-2">
                                Đồng bộ danh mục Page và profile. Bình luận dùng kết nối Meta Graph API của từng Page.
                            </p>

                            <div id="antidetectSummary" class="small text-secondary mb-3">Đang kiểm tra dữ liệu Antidetect...</div>
                            <button class="btn btn-success w-100" id="btnSyncAntidetect" onclick="syncFromAntidetect()">
                                <i class="fa-solid fa-bolt"></i> Đồng bộ toàn bộ Fanpage từ Antidetect
                            </button>
                        </div>

                        <div class="card p-4">
                            <h5 class="fw-bold mb-3"><i class="fa-solid fa-user-plus text-primary"></i> Kết nối Facebook</h5>
                            <div class="mb-3">
                                <label for="accName" class="form-label fw-semibold">Tên gợi nhớ (tùy chọn)</label>
                                <input type="text" id="accName" class="form-control" placeholder="Ví dụ: Nick Bán Hàng 01">
                            </div>
                            <p class="text-muted small">Cấp quyền một lần cho các Page bạn quản lý trong tài khoản này.</p>
                            <label class="form-check mb-2"><input class="form-check-input" id="oauthPublish" type="checkbox"> Kết nối thêm quyền xuất bản</label>
                            <label class="form-check mb-2"><input class="form-check-input" id="oauthInbox" type="checkbox"> Kết nối thêm quyền đọc bình luận khách</label>
                            <button class="btn btn-primary w-100" id="btnFacebookLogin" onclick="connectFacebook()"><i class="fa-solid fa-link"></i> Kết nối Facebook</button>
                            <p id="facebookLoginResult" class="mt-2 small" role="status"></p>
                            <details>
                                <summary>Dùng User Access Token có sẵn</summary>
                                <label class="form-label fw-semibold mt-2" for="accToken">User Access Token</label>
                                <textarea id="accToken" class="form-control mb-2" rows="3" autocomplete="off"></textarea>
                                <button class="btn btn-outline-primary w-100" id="btnConnectAcc" onclick="connectAccount()">Nhập danh sách Page</button>
                            </details>
                        </div>
                    </div>
                    <div class="col-md-7">
                        <div class="card p-4">
                            <h5 class="fw-bold mb-3"><i class="fa-solid fa-list-check text-primary"></i> Danh sách Fanpage (<span id="pageCount">0</span>)</h5>
                            <div class="table-responsive" style="max-height: 620px; overflow-y: auto;">
                                <table class="table align-middle">
                                    <thead class="table-light sticky-top">
                                        <tr>
                                            <th>Tên Fanpage</th>
                                            <th>Nick / Profile nguồn</th>
                                            <th>Chế độ & Trạng thái</th>
                                            <th>Tự động</th>
                                            <th>Thao tác</th>
                                        </tr>
                                    </thead>
                                    <tbody id="pageTableBody">
                                        <tr><td colspan="5" class="text-center text-muted">Đang tải danh sách...</td></tr>
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
            </div>


            <!-- TAB 3: RULES CONFIG -->
            <div class="tab-pane fade" id="tab-rules">
                <div class="card p-4">
                    <h5 class="fw-bold mb-3"><i class="fa-solid fa-clock text-primary"></i> Thiết lập Kịch bản Riêng theo Page</h5>
                    <div class="row g-3">
                        <div class="col-md-4">
                            <label class="form-label fw-semibold">Chọn Page để cấu hình:</label>
                            <select id="rulePageSelect" class="form-select" onchange="loadPageRule()"></select>
                        </div>
                        <div class="col-md-3">
                            <label class="form-label fw-semibold">Thời gian chờ sau bài đăng (Phút):</label>
                                <input type="number" id="ruleDelay" class="form-control" readonly>
                        </div>
                        <div class="col-md-3">
                            <label class="form-label fw-semibold">Giới hạn comment / ngày:</label>
                            <input type="number" id="ruleMaxPerDay" class="form-control" value="20" min="1" max="200">
                        </div>
                    </div>
                    <div class="mt-4">
                        <label class="form-label fw-semibold">Mẫu bình luận từ Sheet</label>
                        <textarea id="ruleTemplates" class="form-control" rows="5" readonly></textarea>
                    </div>
                    <div class="mt-4">
                        <button class="btn btn-success px-4" onclick="savePageRule()"><i class="fa-solid fa-floppy-disk"></i> Lưu cấu hình</button>
                    </div>
                </div>
            </div>

            <!-- TAB 4: MANUAL POSTING -->
            <div class="tab-pane fade" id="tab-manual">
                <div class="card p-4">
                    <h5 class="fw-bold mb-3"><i class="fa-solid fa-pen-to-square text-primary"></i> Soạn & Hẹn giờ Bình luận Thủ công</h5>
                    <div class="row">
                        <div class="col-md-5">
                            <div class="mb-3">
                                <label class="form-label fw-semibold">1. Chọn Fanpage:</label>
                                <select id="manualPageSelect" class="form-select" onchange="loadRecentPostsForManual()"></select>
                            </div>
                            <div class="mb-3">
                                <label class="form-label fw-semibold">2. Chọn bài viết từ danh sách:</label>
                                <div id="recentPostsContainer" style="max-height: 250px; overflow-y: auto;" class="border rounded p-2 bg-light">
                                    <small class="text-muted">Chọn Page để tải bài viết gần nhất...</small>
                                </div>
                            </div>
                            <div class="mb-3">
                                <label class="form-label fw-semibold">Hoặc nhập trực tiếp Post ID / Link bài:</label>
                                <input type="text" id="manualPostId" class="form-control" placeholder="123456_987654 hoặc https://facebook.com/..." oninput="resetManualBatchId()">
                            </div>
                        </div>
                        <div class="col-md-7 border-start">
                            <div class="mb-3">
                                <div class="d-flex justify-content-between align-items-center mb-1">
                                    <label class="form-label fw-semibold mb-0">3. Nội dung bình luận (hỗ trợ Spintax):</label>
                                    <button type="button" class="btn btn-sm btn-outline-secondary" onclick="previewManualSpintax()"><i class="fa-solid fa-dice"></i> Xem thử & Quay lại</button>
                                </div>
                                <textarea id="manualContent" class="form-control" rows="3" placeholder="Nhập nội dung..." oninput="resetManualBatchId()"></textarea>
                                <div id="spintaxPreviewBox" class="alert alert-secondary py-2 px-3 mt-2 small" style="display:none;"></div>
                            </div>
                            <div class="mb-3">
                                <div class="form-check form-check-inline">
                                    <input class="form-check-input" type="radio" name="sendMode" id="modeNow" value="now" checked onchange="toggleScheduleInput()">
                                    <label class="form-check-label fw-semibold" for="modeNow">Đăng ngay</label>
                                </div>
                                <div class="form-check form-check-inline">
                                    <input class="form-check-input" type="radio" name="sendMode" id="modeSchedule" value="schedule" onchange="toggleScheduleInput()">
                                    <label class="form-check-label fw-semibold" for="modeSchedule">Hẹn giờ</label>
                                </div>
                            </div>
                            <div class="mb-3" id="scheduleInputGroup" style="display: none;">
                                <label class="form-label fw-semibold">Thời điểm đăng (GMT+7):</label>
                                <input type="datetime-local" id="manualScheduleTime" class="form-control w-75">
                            </div>
                            <button class="btn btn-primary px-4" id="btnSubmitManual" onclick="createManualTask()"><i class="fa-solid fa-paper-plane"></i> Gửi bình luận</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- TAB 5: TASKS MONITOR -->
            <div class="tab-pane fade" id="tab-tasks">
                <div class="card p-4">
                    <div class="d-flex justify-content-between align-items-center mb-3">
                        <h5 class="fw-bold mb-0"><i class="fa-solid fa-list-ol text-primary"></i> Hàng đợi & Lịch sử Tác vụ</h5>
                        <div class="d-flex gap-2">
                            <select id="filterPage" class="form-select form-select-sm" onchange="loadTasks()">
                                <option value="">Tất cả Fanpage</option>
                            </select>
                            <select id="filterStatus" class="form-select form-select-sm" onchange="loadTasks()">
                                <option value="">Tất cả trạng thái</option>
                                <option value="NEED_CHECK">Cần kiểm tra (Ưu tiên)</option>
                                <option value="PENDING">Chờ lịch</option>
                                <option value="PAUSED">Tạm dừng</option>
                                <option value="SUCCESS">Đã đăng thành công</option>
                                <option value="FAILED">Thất bại</option>
                            </select>
                            <button class="btn btn-sm btn-outline-secondary" onclick="loadTasks()"><i class="fa-solid fa-rotate-right"></i> Làm mới</button>
                        </div>
                    </div>
                    <div class="table-responsive">
                        <table class="table align-middle">
                            <thead class="table-light">
                                <tr>
                                    <th>#ID</th>
                                    <th>Fanpage</th>
                                    <th>Post ID</th>
                                    <th>Nội dung bình luận</th>
                                    <th>Lịch dự kiến</th>
                                    <th>Trạng thái</th>
                                    <th>Thao tác xử lý</th>
                                </tr>
                            </thead>
                            <tbody id="taskTableBody">
                                <tr><td colspan="7" class="text-center text-muted">Đang tải danh sách tác vụ...</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js" integrity="sha384-YvpcrYf0tY3lHB60NNkmXc5s9fDVZLESaAA55NDzOxhy9GkcIdslK1eN7N6jIeHz" crossorigin="anonymous"></script>
    <script>
        let cachedPages = [];
        let dashboardPaused = true;
        let confirmedPreview = null;
        function unlockDashboard() {
            const key = prompt('Nhập khóa DASHBOARD_ADMIN_KEY đã cấu hình trên máy chủ.');
            if (key) {
                sessionStorage.setItem('dashboardAdminKey', key);
                location.reload();
            }
        }
        async function apiFetch(url, options = {}) {
            const key = sessionStorage.getItem('dashboardAdminKey') || '';
            const headers = new Headers(options.headers || {});
            headers.set('X-Dashboard-Key', key);
            const res = await fetch(url, { ...options, headers });
            if (res.status === 401) {
                sessionStorage.removeItem('dashboardAdminKey');
                document.getElementById('unlockBtn').hidden = false;
            }
            return res;
        }

        let currentManualBatchId = generateBatchId();

        function generateBatchId() {
            return 'b_' + Date.now() + '_' + Math.random().toString(36).substring(2, 9);
        }

        function resetManualBatchId() {
            currentManualBatchId = generateBatchId();
        }

        function escapeHtml(text) {
            return String(text || '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        async function fetchMetrics() {
            try {
                const res = await apiFetch('/api/metrics');
                const data = await res.json();
                document.getElementById('mTotalPages').innerText = data.total_pages || 0;
                document.getElementById('mPending').innerText = data.task_counts?.PENDING || 0;
                document.getElementById('mSuccess').innerText = data.task_counts?.SUCCESS || 0;
                document.getElementById('mNeedCheck').innerText = data.task_counts?.NEED_CHECK || 0;

                const credWarn = document.getElementById('appCredWarning');
                credWarn.style.display = data.has_facebook_login_config === false ? 'block' : 'none';

                if (!res.ok) return;
                const sheet = data.metrics.sheet_sync;
                if (sheet) {
                    const input = document.getElementById('sheetUrlInput');
                    if (!input.value && sheet.sheet_url) input.value = sheet.sheet_url;
                    document.getElementById('pilotSheetStatus').textContent =
                        sheet.last_error || (sheet.last_run
                            ? 'Đã đồng bộ ' + sheet.templates_count + ' mẫu lúc ' + new Date(sheet.last_run).toLocaleTimeString('vi-VN')
                            : 'Đang chờ lần đọc đầu tiên.');
                }
                dashboardPaused = data.metrics.paused_all;
                const isPaused = dashboardPaused;
                document.getElementById('pilotStatus').textContent =
                    'Page sẵn sàng tự động: ' + data.active_pages;
                const statusBadge = document.getElementById('systemStatusBadge');
                const pauseBtn = document.getElementById('pauseBtn');
                if (isPaused) {
                    statusBadge.className = 'badge bg-warning text-dark py-2 px-3';
                    statusBadge.innerHTML = '<i class="fa-solid fa-pause"></i> Hệ thống đang tạm dừng';
                    pauseBtn.className = 'btn btn-sm btn-success';
                    pauseBtn.innerHTML = '<i class="fa-solid fa-play"></i> Tiếp tục chạy';
                } else {
                    statusBadge.className = 'badge bg-success py-2 px-3';
                    statusBadge.innerHTML = '<i class="fa-solid fa-circle-check"></i> Hệ thống đang chạy';
                    pauseBtn.className = 'btn btn-sm btn-outline-warning';
                    pauseBtn.innerHTML = '<i class="fa-solid fa-pause"></i> Tạm dừng khẩn cấp';
                }
            } catch(e) {}
        }

        async function togglePause() {
            const res = await apiFetch('/api/system/pause', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ paused: !dashboardPaused })
            });
            if (!res.ok) alert((await res.json()).detail);
            else dashboardPaused = (await res.json()).paused;
            fetchMetrics();
        }

        async function triggerManualSync() {
            const res = await apiFetch('/api/sync', { method: 'POST' });
            const data = await res.json();
            if (!res.ok) {
                alert('Không thể chạy: ' + (data.detail || 'Lỗi không xác định'));
                return;
            }
            alert(data.message);
            setTimeout(() => { loadTasks(); fetchMetrics(); }, 2000);
        }

        async function fetchPages() {
            const res = await apiFetch('/api/pages');
            const data = await res.json();
            cachedPages = data.pages || [];
            document.getElementById('pageCount').innerText = cachedPages.length;

            const tbody = document.getElementById('pageTableBody');
            const ruleSelect = document.getElementById('rulePageSelect');
            const manualSelect = document.getElementById('manualPageSelect');
            const filterPage = document.getElementById('filterPage');

            tbody.innerHTML = '';
            ruleSelect.innerHTML = '';
            manualSelect.innerHTML = '';
            filterPage.innerHTML = '<option value="">Tất cả Fanpage</option>';

            if (cachedPages.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted py-4">Chưa có Page nào. Hãy thêm User Access Token bên trái để nạp Page.</td></tr>';
                return;
            }

            cachedPages.forEach(p => {
                const isPageActive = Number(p.is_active) === 1;
                let badgeToken = '<span class="badge bg-success">Graph API (OK)</span>';
                if (p.connection_mode === 'HYBRID') {
                    badgeToken = '<span class="badge bg-primary">Hybrid (API + Antidetect)</span>';
                } else if (p.connection_mode === 'ANTIDETECT' || p.token_status === 'ANTIDETECT_READY') {
                    badgeToken = '<span class="badge bg-info text-dark">Sẵn sàng (Antidetect CDP)</span>';
                }
                if (p.token_status === 'INVALID_SECRET') badgeToken = '<span class="badge bg-danger">Lỗi khóa mã hóa / Cần kết nối lại</span>';
                else if (p.token_status === 'EXPIRED') badgeToken = '<span class="badge bg-danger">Hết hạn / Cần đăng nhập lại</span>';
                else if (p.token_status === 'MISSING_PERMISSIONS') badgeToken = '<span class="badge bg-warning text-dark">Thiếu quyền Page</span>';
                else if (p.token_status === 'EXPIRING_SOON') badgeToken = '<span class="badge bg-warning text-dark">Sắp hết hạn</span>';

                const srcLabel = p.antidetect_profile_name || p.account_name || 'N/A';
                const activeBadge = isPageActive
                    ? '<span class="badge bg-success">Đang hoạt động</span>'
                    : '<span class="badge bg-danger">Đã khóa</span>';
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td><strong>${escapeHtml(p.page_name)}</strong><br><small class="text-muted">ID: ${escapeHtml(p.page_id)}</small></td>
                    <td><span class="badge bg-light text-dark border text-wrap text-start">${escapeHtml(srcLabel)}</span></td>
                    <td>${activeBadge}<br>${badgeToken}<br><small class="text-muted">${escapeHtml(p.connection_error || p.send_error || p.scan_error || p.status_note || '')}</small></td>
                    <td>${p.rule_enabled ? 'Đã bật' : 'Tạm dừng'}<br><small>${p.delay_minutes ?? '—'} phút · Đã dùng ${p.comments_used_today}/${p.max_comments_per_day ?? '—'}, còn ${p.comments_remaining_today}<br>${escapeHtml(p.source_error || p.send_wait_reason || '')}${p.next_send_at ? '<br>Thử tiếp: ' + escapeHtml(new Date(p.next_send_at).toLocaleString('vi-VN', {timeZone:'Asia/Bangkok'})) : ''}</small></td>
                    <td></td>
                `;
                const actions = [
                    ['Kiểm tra Meta', 'check', Boolean(p.has_page_token)],
                    ['Xem mẫu', 'preview', true],
                    [p.rule_enabled ? 'Tạm dừng' : 'Bật tự động', 'automation', Boolean(p.rule_enabled || (isPageActive && p.has_page_token))],
                ];
                if (!isPageActive && p.has_page_token) actions.push(['Kích hoạt kết nối', 'reactivate', true]);
                for (const [label, action, enabled] of actions) {
                    const button = document.createElement('button');
                    button.className = 'btn btn-sm btn-outline-primary me-1 mb-1';
                    button.textContent = label;
                    button.disabled = !enabled;
                    button.onclick = async () => {
                        button.disabled = true;
                        try { await pageAction(p, action); }
                        catch (error) { alert(error.message); }
                        finally { button.disabled = !enabled; }
                    };
                    tr.lastElementChild.appendChild(button);
                }
                tbody.appendChild(tr);

                const modeTag = p.connection_mode === 'ANTIDETECT' ? '[CDP]' : (p.connection_mode === 'HYBRID' ? '[Hybrid]' : '[API]');
                ruleSelect.innerHTML += `<option value="${escapeHtml(p.page_id)}">${escapeHtml(p.page_name)} ${modeTag} (${escapeHtml(p.page_id)})</option>`;
                if (p.graph_ready) manualSelect.innerHTML += `<option value="${escapeHtml(p.page_id)}">${escapeHtml(p.page_name)} ${modeTag}</option>`;
                filterPage.innerHTML += `<option value="${escapeHtml(p.page_id)}">${escapeHtml(p.page_name)}</option>`;
            });

            loadPageRule();
            loadRecentPostsForManual();
        }

        async function pageAction(page, action) {
            const body = action === 'automation' ? JSON.stringify({ enabled: !page.rule_enabled }) : undefined;
            const res = await apiFetch(`/api/pages/${encodeURIComponent(page.page_id)}/${action}`, {
                method: action === 'preview' ? 'GET' : 'POST',
                headers: body ? { 'Content-Type': 'application/json' } : {},
                body,
            });
            const data = await res.json();
            if (action !== 'preview') { fetchPages(); fetchMetrics(); }
            if (!res.ok) throw new Error(data.detail || 'Không thực hiện được thao tác.');
            if (action === 'preview') {
                const sequence = data.comment_sequence || [];
                const lines = sequence.length
                    ? sequence.map((step, index) => '#' + (index + 1) + ' · Sau ' + step.delay_minutes + ' phút · ' + step.template)
                    : ['Chờ ' + data.delay_minutes + ' phút', ...(data.templates || [])];
                alert([page.page_name, data.sheet_url, data.source_error, ...lines].filter(Boolean).join(String.fromCharCode(10, 10)));
            } else if (data.message) {
                alert(data.message);
            }
        }

        async function reactivatePage(pageId) {
            if (!confirm('Kích hoạt lại Fanpage này? Hệ thống sẽ quét bài mới và có thể gửi bình luận theo cấu hình.')) return;
            const res = await apiFetch(`/api/pages/${encodeURIComponent(pageId)}/reactivate`, { method: 'POST' });
            const data = await res.json();
            if (res.ok) {
                alert(data.message);
                fetchPages();
                fetchMetrics();
            } else {
                alert('Lỗi: ' + (data.detail || 'Không thể kích hoạt lại Page'));
            }
        }

        async function fetchAntidetectStatus() {
            try {
                const res = await apiFetch('/api/antidetect/status');
                const data = await res.json();
                const el = document.getElementById('antidetectSummary');
                if (!el) return;
                if (data.available) {
                    el.innerHTML = `<i class="fa-solid fa-circle-check text-success"></i> Đã tìm thấy <strong>${data.total_profiles} Profile</strong> và <strong>${data.total_fanpages} Fanpage</strong> trong Antidetect Python.`;
                } else {
                    el.innerHTML = `<i class="fa-solid fa-triangle-exclamation text-warning"></i> Không tìm thấy dữ liệu tại <code>${escapeHtml(data.antidetect_dir)}</code>`;
                }
            } catch (e) {}
        }

        async function syncFromAntidetect() {
            const btn = document.getElementById('btnSyncAntidetect');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Đang đồng bộ từ Antidetect...';
            try {
                const res = await apiFetch('/api/antidetect/sync', { method: 'POST' });
                const data = await res.json();
                if (res.ok) {
                    alert(data.message);
                    fetchPages();
                    fetchMetrics();
                    fetchAntidetectStatus();
                } else {
                    alert('Lỗi: ' + (data.detail || 'Không thể đồng bộ'));
                }
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-bolt"></i> Đồng bộ toàn bộ Fanpage từ Antidetect';
            }
        }


        async function connectFacebook() {
            const button = document.getElementById('btnFacebookLogin');
            const hint = document.getElementById('facebookLoginResult');
            button.disabled = true;
            hint.textContent = '';
            try {
                const response = await apiFetch('/api/accounts/facebook/start', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        account_name: document.getElementById('accName').value.trim(),
                        publish_access: document.getElementById('oauthPublish').checked,
                        inbox_access: document.getElementById('oauthInbox').checked,
                    }),
                });
                const result = await response.json();
                if (!response.ok) throw new Error(result.detail || 'Không thể mở đăng nhập Facebook.');
                location.assign(result.url);
            } catch (error) {
                hint.textContent = error.message;
                button.disabled = false;
            }
        }

        async function connectAccount() {
            const name = document.getElementById('accName').value.trim();
            const token = document.getElementById('accToken').value.trim();
            if (!token) { alert('Vui lòng nhập User Access Token.'); return; }

            const btn = document.getElementById('btnConnectAcc');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Đang xác thực...';

            try {
                const res = await apiFetch('/api/accounts/connect', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ account_name: name, user_access_token: token })
                });
                const data = await res.json();
                if (res.ok) {
                    let msg = data.message;
                    if (data.warnings && data.warnings.length) {
                        msg += '\\n\\nLưu ý:\\n- ' + data.warnings.join('\\n- ');
                    }
                    alert(msg);
                    document.getElementById('accName').value = '';
                    document.getElementById('accToken').value = '';
                    fetchPages();
                    fetchMetrics();
                } else {
                    alert('Lỗi: ' + (data.detail || 'Không thể kết nối'));
                }
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-link"></i> Kết nối & Quét Fanpage';
            }
        }

        async function syncGoogleSheet() {
            const url = document.getElementById('sheetUrlInput').value.trim();
            if (!url) { alert('Vui lòng dán link Google Sheet!'); return; }

            const btn = document.getElementById('btnSyncSheet');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Đang đọc Sheet...';

            try {
                const res = await apiFetch('/api/sheets/sync', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ sheet_url: url })
                });
                const data = await res.json();
                const reportBox = document.getElementById('syncReportBox');
                if (res.ok) {
                    const hasErrors = data.errors && data.errors.length > 0;
                    const alertClass = hasErrors ? 'alert-warning' : 'alert-success';
                    let html = `<div class="alert ${alertClass} py-2 mb-0"><strong>${escapeHtml(data.message)}</strong>`;
                    if (hasErrors) {
                        html += '<div class="mt-2 text-danger fw-semibold small">Các dòng lỗi bị bỏ qua:</div><ul class="mb-1 text-danger small">';
                        data.errors.forEach(err => html += `<li>${escapeHtml(err)}</li>`);
                        html += '</ul>';
                    }
                    if (data.warnings && data.warnings.length) {
                        html += '<div class="mt-1 fw-semibold small">Cảnh báo:</div><ul class="mb-0 small">';
                        data.warnings.forEach(w => html += `<li>${escapeHtml(w)}</li>`);
                        html += '</ul>';
                    }
                    html += '</div>';
                    reportBox.innerHTML = html;
                    reportBox.style.display = 'block';
                    fetchPages();
                    fetchMetrics();
                } else {
                    reportBox.innerHTML = `<div class="alert alert-danger py-2 mb-0"><strong>Lỗi:</strong> ${escapeHtml(data.detail)}</div>`;
                    reportBox.style.display = 'block';
                }
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-arrows-rotate"></i> Đồng bộ ngay';
            }
        }

        function loadPageRule() {
            const pageId = document.getElementById('rulePageSelect').value;
            const page = cachedPages.find(p => p.page_id === pageId);
            if (!page) return;

            document.getElementById('ruleDelay').value = page.delay_minutes || 10;
            document.getElementById('ruleMaxPerDay').value = page.max_comments_per_day || 20;
            try {
                const arr = JSON.parse(page.templates || '[]');
                document.getElementById('ruleTemplates').value = arr.join('\\n');
            } catch(e) {
                document.getElementById('ruleTemplates').value = page.templates || '';
            }
        }

        async function savePageRule() {
            const pageId = document.getElementById('rulePageSelect').value;
            const maxPerDay = parseInt(document.getElementById('ruleMaxPerDay').value) || 20;

            const res = await apiFetch(`/api/pages/${encodeURIComponent(pageId)}/rule`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ max_comments_per_day: maxPerDay })
            });
            const data = await res.json();
            if (res.ok) {
                alert(data.message || 'Đã lưu cấu hình kịch bản!');
                fetchPages();
            } else {
                alert('Lỗi: ' + data.detail);
            }
        }

        async function loadRecentPostsForManual() {
            const pageId = document.getElementById('manualPageSelect').value;
            const container = document.getElementById('recentPostsContainer');
            if (!pageId) return;

            container.innerHTML = '<small class="text-muted"><i class="fa-solid fa-spinner fa-spin"></i> Đang tải bài viết...</small>';
            try {
                const res = await apiFetch(`/api/pages/${pageId}/recent_posts`);
                const data = await res.json();
                if (!res.ok) {
                    container.innerHTML = `<small class="text-danger">${escapeHtml(data.detail || 'Không thể tải bài viết')}</small>`;
                    return;
                }
                const posts = data.posts || [];
                if (posts.length === 0) {
                    container.innerHTML = '<small class="text-muted">Không có bài viết gần đây.</small>';
                    return;
                }
                container.innerHTML = '';
                posts.forEach(pt => {
                    const d = document.createElement('div');
                    d.className = 'post-card';
                    const text = pt.message ? pt.message.substring(0, 90) + '...' : (pt.permalink_url || '(Bài viết trên Fanpage)');
                    const time = new Date(pt.created_time).toLocaleString('vi-VN');
                    const clickVal = pt.id;
                    d.innerHTML = `<div><strong>${escapeHtml(text)}</strong></div><small class="text-muted">Đăng lúc: ${escapeHtml(time)} | ID: <code>${escapeHtml(pt.id)}</code></small>`;
                    d.onclick = () => {
                        document.querySelectorAll('.post-card').forEach(el => el.classList.remove('selected'));
                        d.classList.add('selected');
                        document.getElementById('manualPostId').value = clickVal;
                        resetManualBatchId();
                    };
                    container.appendChild(d);
                });
            } catch(e) {
                container.innerHTML = '<small class="text-danger">Không thể tải danh sách bài.</small>';
            }
        }


        async function previewManualSpintax() {
            const content = document.getElementById('manualContent').value.trim();
            const box = document.getElementById('spintaxPreviewBox');
            if (!content) { alert('Vui lòng nhập nội dung bình luận để xem thử!'); return; }
            const res = await apiFetch('/api/spintax/preview', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content })
            });
            const data = await res.json();
            box.style.display = 'block';
            if (res.ok) {
                confirmedPreview = { source: content, value: data.preview };
                box.className = 'alert alert-secondary py-2 px-3 mt-2 small';
                box.innerHTML = `<strong>Xem trước câu sẽ đăng:</strong> ${escapeHtml(data.preview)}`;
            } else {
                box.className = 'alert alert-danger py-2 px-3 mt-2 small';
                box.innerHTML = `<strong>Lỗi:</strong> ${escapeHtml(data.detail)}`;
            }
        }

        function toggleScheduleInput() {
            const isSchedule = document.getElementById('modeSchedule').checked;
            document.getElementById('scheduleInputGroup').style.display = isSchedule ? 'block' : 'none';
        }

        async function createManualTask() {
            const pageId = document.getElementById('manualPageSelect').value;
            const postId = document.getElementById('manualPostId').value.trim();
            const content = document.getElementById('manualContent').value.trim();
            const isNow = document.getElementById('modeNow').checked;
            const schedTime = document.getElementById('manualScheduleTime').value;

            if (!postId || !content) { alert('Vui lòng chọn bài viết và nhập nội dung bình luận!'); return; }

            if (/[{}]/.test(content) && (!confirmedPreview || confirmedPreview.source !== content)) {
                alert('Hãy xem trước nội dung trước khi tạo tác vụ.');
                return;
            }
            const finalContent = confirmedPreview?.source === content ? confirmedPreview.value : content;

            let schedIso = null;
            if (!isNow) {
                if (!schedTime) { alert('Vui lòng chọn thời gian hẹn giờ!'); return; }
                schedIso = new Date(schedTime + ':00+07:00').toISOString();
            }

            const btn = document.getElementById('btnSubmitManual');
            btn.disabled = true;

            try {
                const res = await apiFetch('/api/tasks/manual', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        page_id: pageId,
                        post_id_or_url: postId,
                        content: finalContent,
                        post_now: isNow,
                        scheduled_at: schedIso,
                        batch_id: currentManualBatchId
                    })
                });
                const data = await res.json();
                if (res.ok) {
                    alert('Tạo tác vụ thành công!\\nNội dung: ' + data.content_preview);
                    document.getElementById('manualContent').value = '';
                    document.getElementById('spintaxPreviewBox').style.display = 'none';
                    resetManualBatchId();
                    loadTasks();
                    fetchMetrics();
                } else {
                    alert('Lỗi: ' + data.detail);
                }
            } finally {
                btn.disabled = false;
            }
        }

        async function loadTasks() {
            const statusFilter = document.getElementById('filterStatus').value;
            const pageFilter = document.getElementById('filterPage').value;
            let url = '/api/tasks?limit=50';
            if (statusFilter) url += `&status=${encodeURIComponent(statusFilter)}`;
            if (pageFilter) url += `&page_id=${encodeURIComponent(pageFilter)}`;

            const res = await apiFetch(url);
            const data = await res.json();
            const tasks = data.tasks || [];
            document.getElementById('taskCount').innerText = tasks.length;

            const tbody = document.getElementById('taskTableBody');
            tbody.innerHTML = '';
            if (tasks.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-4">Chưa có tác vụ nào trong danh mục này.</td></tr>';
                return;
            }

            tasks.forEach(t => {
                const dateStr = new Date(t.scheduled_at).toLocaleString('vi-VN', { timeZone: 'Asia/Bangkok' });
                const tr = document.createElement('tr');
                let actionBtns = '';

                if (t.status === 'NEED_CHECK') {
                    actionBtns = `
                        <button class="btn btn-sm btn-success me-1" onclick="resolveTask(${t.id})" title="Đã đối chiếu trên Facebook thấy có bình luận"><i class="fa-solid fa-check"></i> Đã đăng</button>
                        <button class="btn btn-sm btn-outline-primary me-1" onclick="retryTask(${t.id}, true)" title="Chỉ gửi lại sau khi kiểm tra"><i class="fa-solid fa-rotate-right"></i> Gửi lại</button>
                    `;
                } else if (t.status === 'PENDING' || t.status === 'PAUSED') {
                    actionBtns = `<button class="btn btn-sm btn-outline-danger me-1" onclick="cancelTask(${t.id})">Hủy</button>`;
                    if (t.status === 'PAUSED') {
                        actionBtns += `<button class="btn btn-sm btn-outline-primary" onclick="retryTask(${t.id})">Chạy tiếp</button>`;
                    }
                } else if (t.status === 'FAILED') {
                    actionBtns = `<button class="btn btn-sm btn-outline-primary" onclick="retryTask(${t.id})"><i class="fa-solid fa-rotate-right"></i> Thử lại</button>`;
                }

                if (t.comment_id && t.comment_id !== 'MANUAL_VERIFIED') {
                    actionBtns += `<a href="https://facebook.com/${escapeHtml(t.comment_id)}" target="_blank" class="btn btn-sm btn-link"><i class="fa-solid fa-arrow-up-right-from-square"></i></a>`;
                }

                tr.innerHTML = `
                    <td>#${t.id}</td>
                    <td><strong>${escapeHtml(t.page_name)}</strong><br><small class="text-muted">${t.source === 'AUTO' ? '<i class="fa-solid fa-robot"></i> Tự động' : '<i class="fa-solid fa-user"></i> Thủ công'}${t.sequence_step ? ' · Bình luận #' + Number(t.sequence_step) : ''}</small></td>
                    <td><code>${escapeHtml(t.post_id)}</code></td>
                    <td><div style="max-width:240px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${escapeHtml(t.comment_content)}">${escapeHtml(t.comment_content)}</div></td>
                    <td><small>${escapeHtml(dateStr)}</small></td>
                    <td><span class="badge badge-status badge-${escapeHtml(t.status)}">${escapeHtml(t.status)}</span><br><small class="text-danger">${escapeHtml(t.error_message || '')}</small></td>
                    <td>${actionBtns}</td>
                `;
                tbody.appendChild(tr);
            });
        }

        async function cancelTask(id) {
            if (!confirm('Bạn chắc chắn muốn hủy tác vụ này?')) return;
            const res = await apiFetch(`/api/tasks/${id}/cancel`, { method: 'POST' });
            if (!res.ok) {
                const data = await res.json();
                alert(data.detail || 'Không thể hủy tác vụ');
            }
            loadTasks();
            fetchMetrics();
        }

        async function retryTask(id, needCheck = false) {
            if (needCheck && !confirm('Bạn đã mở bài trên Facebook và xác nhận Page chưa đăng bình luận này?')) return;
            const res = await apiFetch(`/api/tasks/${id}/retry`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirmed_not_posted: needCheck }) });
            if (!res.ok) {
                const data = await res.json();
                alert(data.detail || 'Không thể gửi lại tác vụ');
            }
            loadTasks();
            fetchMetrics();
        }

        async function resolveTask(id) {
            const commentId = prompt('Nhập ID bình luận trên Facebook (nếu có, để trống cũng được):', '');
            if (commentId === null) return;
            const res = await apiFetch(`/api/tasks/${id}/resolve`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ comment_id: commentId })
            });
            if (!res.ok) {
                const data = await res.json();
                alert(data.detail || 'Không thể cập nhật tác vụ');
            }
            loadTasks();
            fetchMetrics();
        }

        window.onload = () => {
            const query = new URLSearchParams(location.search);
            const login = query.get('facebook_login');
            if (login) {
                const count = Number(query.get('pages')) || 0;
                const messages = {
                    connected: 'Đã kết nối Facebook. Số Page nhập: ' + count,
                    check: 'Đã nhập ' + count + ' Page. Kiểm tra kết nối của từng Page trước khi bật.',
                    invalid: 'Phiên kết nối đã hết hạn hoặc không hợp lệ. Vui lòng kết nối lại.',
                    cancelled: 'Bạn đã hủy hoặc chưa cấp quyền kết nối.',
                    failed: 'Kết nối Facebook thất bại. Kiểm tra quyền và địa chỉ callback của Meta App.',
                };
                document.getElementById('facebookLoginResult').textContent = messages[login] || '';
                document.querySelector('[data-bs-target="#tab-pages"]')?.click();
                history.replaceState(null, '', location.pathname);
            }
            fetchMetrics();
            fetchAntidetectStatus();
            fetchPages();
            loadTasks();
            setInterval(fetchMetrics, 10000);
        };

    </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)
