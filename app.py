import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

import config
import database as db
import meta_client
import scheduler
import sheets_sync
import spintax


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB schema & startup recovery
    db.init_db()
    # Start 3 background loops
    task1 = asyncio.create_task(scheduler.loop_post_scanner())
    task2 = asyncio.create_task(scheduler.loop_task_executor())
    task3 = asyncio.create_task(scheduler.loop_maintenance())
    yield
    task1.cancel()
    task2.cancel()
    task3.cancel()


app = FastAPI(title="Facebook Page Multi-Account Commenter Hub", lifespan=lifespan)


# --- Pydantic Request Models ---
class ConnectAccountRequest(BaseModel):
    account_name: str
    user_access_token: str


class SyncSheetRequest(BaseModel):
    sheet_url: str


class UpdateRuleRequest(BaseModel):
    delay_minutes: int
    templates: List[str]
    max_comments_per_day: int = 20
    is_enabled: bool


class ManualTaskRequest(BaseModel):
    page_id: str
    post_id_or_url: str
    content: str
    post_now: bool = True
    scheduled_at: Optional[str] = None
    batch_id: Optional[str] = None


class SpintaxPreviewRequest(BaseModel):
    content: str


class ResolveTaskRequest(BaseModel):
    comment_id: Optional[str] = None


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
        "active_pages": sum(1 for p in pages if p.get("is_active") and p.get("rule_enabled")),
        "has_app_credentials": config.has_app_credentials()
    }


@app.post("/api/system/toggle_pause")
def toggle_pause():
    scheduler.SCHEDULER_METRICS["paused_all"] = not scheduler.SCHEDULER_METRICS["paused_all"]
    state = "TẠM DỪNG" if scheduler.SCHEDULER_METRICS["paused_all"] else "ĐANG CHẠY"
    return {
        "success": True,
        "paused": scheduler.SCHEDULER_METRICS["paused_all"],
        "message": f"Hệ thống hiện {state}"
    }


@app.post("/api/sync")
def trigger_manual_sync():
    scheduler.detect_new_posts_and_schedule()
    scheduler.execute_due_tasks()
    return {"success": True, "message": "Đã quét bài mới và thực thi các tác vụ đến hạn."}


@app.get("/api/pages")
def list_pages():
    pages = db.get_all_pages()
    return {"pages": pages}


@app.get("/api/pages/{page_id}/recent_posts")
def get_page_posts(page_id: str):
    page = db.get_page_with_decrypted_token(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Không tìm thấy Page")
    token = page.get("page_access_token")
    if not token:
        raise HTTPException(status_code=400, detail="Page chưa có token hợp lệ")
    try:
        posts = meta_client.get_recent_posts(page_id, token, limit=15)
        return {"posts": posts}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không thể tải bài viết: {str(e)}")


@app.post("/api/pages/{page_id}/resume_tasks")
def resume_page_tasks(page_id: str):
    page = db.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Không tìm thấy Page")
    resumed = db.resume_paused_tasks_for_page(page_id)
    return {"success": True, "resumed_count": resumed, "message": f"Đã khôi phục {resumed} tác vụ tạm dừng về hàng đợi."}


@app.post("/api/accounts/connect")
def connect_account(req: ConnectAccountRequest):
    """
    Connects a Facebook Account using User Access Token.
    Exchanges short token for long-lived token, checks scopes, and saves pages securely.
    """
    token = req.user_access_token.strip()
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
            raise HTTPException(status_code=500, detail=f"Lỗi khi đổi sang Token dài hạn: {str(e)}")

    # 2. Get User Info
    try:
        user_info = meta_client.get_user_info(long_lived_token)
        user_id = str(user_info.get("id")) if user_info.get("id") else None
    except Exception:
        user_id = None

    # 3. Get Pages
    try:
        pages = meta_client.get_user_pages(long_lived_token)
    except meta_client.MetaAPIError as e:
        raise HTTPException(status_code=400, detail=e.message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Không thể kết nối Meta API: {str(e)}")

    if not pages:
        return {
            "success": True,
            "message": "Không tìm thấy Page nào do tài khoản này quản lý.",
            "pages_added": 0,
            "warnings": warnings
        }

    # Upsert account record
    acc_id = db.upsert_account(
        account_name=req.account_name,
        user_id=user_id,
        long_lived_user_token=long_lived_token
    )

    added = 0
    for p in pages:
        page_id = str(p.get("id"))
        page_name = p.get("name")
        page_token = p.get("access_token")
        if page_id and page_token:
            info = meta_client.debug_token(page_token)
            if info.get("checked") is True:
                t_status = info.get("status", "OK")
                missing_scopes = info.get("missing_scopes", [])
                expires_at = info.get("expires_at")
            else:
                t_status = "OK"
                missing_scopes = []
                expires_at = None

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

    msg = f"Đã kết nối thành công tài khoản '{req.account_name}' và nạp {added} Fanpage."
    return {"success": True, "message": msg, "pages_added": added, "warnings": warnings}


@app.post("/api/sheets/sync")
def sync_google_sheet(req: SyncSheetRequest):
    result = sheets_sync.sync_from_google_sheet(req.sheet_url)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Lỗi đồng bộ Google Sheet"))
    # Save URL for periodic auto-sync
    scheduler.SCHEDULER_METRICS["sheet_sync"]["sheet_url"] = req.sheet_url.strip()
    scheduler.SCHEDULER_METRICS["sheet_sync"]["last_run"] = datetime.now(timezone.utc).isoformat()
    scheduler.SCHEDULER_METRICS["sheet_sync"]["last_error"] = None
    return result


@app.post("/api/pages/{page_id}/rule")
def update_rule(page_id: str, req: UpdateRuleRequest):
    page = db.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Không tìm thấy Page")
    for idx, tpl in enumerate(req.templates, start=1):
        if not sheets_sync.validate_spintax_braces(tpl):
            raise HTTPException(
                status_code=400,
                detail=f"Mẫu bình luận dòng {idx} bị lỗi đóng/mở ngoặc nhọn Spintax '{{...}}'."
            )
    db.update_page_rule(page_id, req.delay_minutes, req.templates, req.max_comments_per_day, req.is_enabled)
    return {"success": True, "message": "Đã cập nhật kịch bản bình luận thành công"}


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
    token = page.get("page_access_token", "")

    raw_content = req.content.strip()
    if not raw_content:
        raise HTTPException(status_code=400, detail="Nội dung bình luận không được để trống")
    if not sheets_sync.validate_spintax_braces(raw_content):
        raise HTTPException(status_code=400, detail="Nội dung bình luận bị lỗi đóng/mở ngoặc nhọn Spintax '{...}'")

    # 1. Verify post belongs to this page
    try:
        post_data = meta_client.verify_post_belongs_to_page(req.post_id_or_url, req.page_id, token)
        verified_post_id = post_data["clean_post_id"]
    except meta_client.MetaAPIError as e:
        raise HTTPException(status_code=400, detail=f"Xác minh bài viết thất bại: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Lỗi kiểm tra bài viết: {str(e)}")

    # 2. Spin content immediately so user gets consistent output
    spun_content = spintax.spin(raw_content)

    # 3. Calculate target time
    if req.post_now:
        sched_time = datetime.now(timezone.utc).isoformat()
    else:
        if not req.scheduled_at:
            raise HTTPException(status_code=400, detail="Vui lòng chọn thời gian hẹn giờ")
        sched_time = req.scheduled_at

    batch_id = (req.batch_id or "").strip() or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    success = db.create_task(
        page_id=req.page_id,
        post_id=verified_post_id,
        comment_content=spun_content,
        scheduled_at=sched_time,
        source="MANUAL",
        batch_id=batch_id
    )
    if not success:
        raise HTTPException(status_code=400, detail="Tác vụ này đã tồn tại trong hàng đợi (chống gửi trùng).")

    return {"success": True, "message": "Đã tạo tác vụ bình luận thành công!", "content_preview": spun_content}


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
def retry_task(task_id: int):
    success = db.retry_task(task_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Không thể gửi lại tác vụ này (chỉ gửi lại được tác vụ Lỗi, Cần kiểm tra, Tạm dừng hoặc Đã hủy; không gửi lại tác vụ Đã đăng hoặc Đang gửi)."
        )
    return {"success": True, "message": "Đã dời tác vụ về trạng thái Chờ gửi ngay"}


@app.post("/api/tasks/{task_id}/resolve")
def resolve_task(task_id: int, req: ResolveTaskRequest):
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
                <span id="systemStatusBadge" class="badge bg-success py-2 px-3"><i class="fa-solid fa-circle-check"></i> Hệ thống đang chạy</span>
                <button id="pauseBtn" class="btn btn-sm btn-outline-warning" onclick="togglePause()"><i class="fa-solid fa-pause"></i> Tạm dừng khẩn cấp</button>
            </div>
        </div>
    </nav>

    <div class="container my-3">
        <!-- App Credentials Warning Banner -->
        <div id="appCredWarning" class="alert alert-warning py-2 mb-3" style="display: none;">
            <i class="fa-solid fa-triangle-exclamation"></i>
            <strong>Lưu ý cấu hình:</strong> Bạn chưa điền <code>META_APP_ID</code> và <code>META_APP_SECRET</code> trong file <code>.env</code>.
            Hãy bổ sung trước khi kết nối Nick thật để tool tự động đổi sang Token dài hạn (~60 ngày / vĩnh viễn cho Page).
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
        </ul>

        <div class="tab-content">
            <!-- TAB 1: GOOGLE SHEETS SYNC -->
            <div class="tab-pane fade show active" id="tab-sheets">
                <div class="card p-4">
                    <h5 class="fw-bold text-success mb-2"><i class="fa-solid fa-file-excel"></i> Quản lý Cấu hình & Mẫu Comment qua Google Sheet</h5>
                    <p class="text-muted small">
                        <strong>Bảo mật 100%:</strong> Sheet này <u>KHÔNG</u> chứa Token. Token được quản lý riêng trong tool để đảm bảo an toàn nếu bạn gửi link Sheet cho người khác. Sau khi đồng bộ lần đầu, tool sẽ tự động đồng bộ lại mỗi 5 phút.
                    </p>
                    <div class="input-group mb-3">
                        <span class="input-group-text bg-white"><i class="fa-solid fa-link text-muted"></i></span>
                        <input type="text" id="sheetUrlInput" class="form-control" placeholder="Dán link Google Sheet (ví dụ: https://docs.google.com/spreadsheets/d/.../edit)">
                        <button class="btn btn-success px-4" id="btnSyncSheet" onclick="syncGoogleSheet()"><i class="fa-solid fa-arrows-rotate"></i> Đồng bộ ngay</button>
                    </div>

                    <div id="syncReportBox" style="display:none;" class="mb-3"></div>

                    <div class="alert alert-info py-3 mb-0">
                        <h6 class="fw-bold mb-2"><i class="fa-solid fa-circle-question"></i> Hướng dẫn cấu trúc 2 Tab trong Google Sheet:</h6>
                        <div class="row">
                            <div class="col-md-6 border-end">
                                <strong>Tab 1: <code>Pages</code></strong>
                                <ul class="small mb-0 mt-1">
                                    <li><code>Page ID</code>: ID Page dạng số (nên thêm dấu <code>'</code> ở đầu, vd: <code>'1029384756</code>)</li>
                                    <li><code>Tên gợi nhớ</code>: Tên nhận diện Fanpage</li>
                                    <li><code>Delay (phút)</code>: Số phút chờ comment sau khi có bài mới (1–1440)</li>
                                    <li><code>Bật tự động</code>: <code>TRUE</code> hoặc <code>FALSE</code></li>
                                    <li><code>Giới hạn comment/ngày</code>: Tối đa số comment/ngày (vd: 20)</li>
                                </ul>
                            </div>
                            <div class="col-md-6">
                                <strong>Tab 2: <code>Comments</code></strong>
                                <ul class="small mb-0 mt-1">
                                    <li><code>Page ID</code>: ID Fanpage khớp với Tab Pages</li>
                                    <li><code>Mẫu bình luận</code>: Nội dung comment (hỗ trợ Spintax dạng <code>{Chào|Xin chào} bạn</code>)</li>
                                    <li><code>Bật</code>: <code>TRUE</code> hoặc <code>FALSE</code></li>
                                </ul>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- TAB 2: PAGES & ACCOUNTS -->
            <div class="tab-pane fade" id="tab-pages">
                <div class="row">
                    <div class="col-md-5">
                        <div class="card p-4">
                            <h5 class="fw-bold mb-3"><i class="fa-solid fa-user-plus text-primary"></i> Kết nối Nick Facebook mới</h5>
                            <p class="text-muted small">Dán User Token của nick bất kỳ. Hệ thống sẽ đổi sang Token dài hạn, kiểm tra quyền và mã hóa trước khi lưu.</p>
                            <div class="mb-3">
                                <label class="form-label fw-semibold">Tên gợi nhớ cho Nick:</label>
                                <input type="text" id="accName" class="form-control" placeholder="Ví dụ: Nick Bán Hàng 01">
                            </div>
                            <div class="mb-3">
                                <label class="form-label fw-semibold">User Access Token:</label>
                                <textarea id="accToken" class="form-control" rows="3" placeholder="EAAB..."></textarea>
                            </div>
                            <button class="btn btn-primary w-100" id="btnConnectAcc" onclick="connectAccount()"><i class="fa-solid fa-link"></i> Kết nối & Quét Fanpage</button>
                        </div>
                    </div>
                    <div class="col-md-7">
                        <div class="card p-4">
                            <h5 class="fw-bold mb-3"><i class="fa-solid fa-list-check text-primary"></i> Danh sách Fanpage (<span id="pageCount">0</span>)</h5>
                            <div class="table-responsive">
                                <table class="table align-middle">
                                    <thead class="table-light">
                                        <tr>
                                            <th>Tên Fanpage</th>
                                            <th>Nick nguồn</th>
                                            <th>Tình trạng Token</th>
                                            <th>Tự động</th>
                                        </tr>
                                    </thead>
                                    <tbody id="pageTableBody">
                                        <tr><td colspan="4" class="text-center text-muted">Đang tải danh sách...</td></tr>
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
                            <input type="number" id="ruleDelay" class="form-control" value="10" min="1" max="1440">
                        </div>
                        <div class="col-md-3">
                            <label class="form-label fw-semibold">Giới hạn comment / ngày:</label>
                            <input type="number" id="ruleMaxPerDay" class="form-control" value="20" min="1" max="200">
                        </div>
                        <div class="col-md-2">
                            <label class="form-label fw-semibold">Bật/Tắt:</label>
                            <div class="form-check form-switch mt-2">
                                <input class="form-check-input" type="checkbox" id="ruleEnabled" checked>
                                <label class="form-check-label" for="ruleEnabled">Bật</label>
                            </div>
                        </div>
                    </div>
                    <div class="mt-4">
                        <label class="form-label fw-semibold">Danh sách Mẫu Bình luận (Mỗi dòng 1 câu, hỗ trợ Spintax):</label>
                        <textarea id="ruleTemplates" class="form-control" rows="5"></textarea>
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

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        let cachedPages = [];
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
                const res = await fetch('/api/metrics');
                const data = await res.json();
                document.getElementById('mTotalPages').innerText = data.total_pages || 0;
                document.getElementById('mPending').innerText = data.task_counts?.PENDING || 0;
                document.getElementById('mSuccess').innerText = data.task_counts?.SUCCESS || 0;
                document.getElementById('mNeedCheck').innerText = data.task_counts?.NEED_CHECK || 0;

                const credWarn = document.getElementById('appCredWarning');
                credWarn.style.display = data.has_app_credentials === false ? 'block' : 'none';

                const isPaused = data.metrics?.paused_all;
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
            await fetch('/api/system/toggle_pause', { method: 'POST' });
            fetchMetrics();
        }

        async function triggerManualSync() {
            const res = await fetch('/api/sync', { method: 'POST' });
            const data = await res.json();
            alert(data.message);
            loadTasks();
            fetchMetrics();
        }

        async function fetchPages() {
            const res = await fetch('/api/pages');
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
                tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted py-4">Chưa có Page nào. Hãy thêm User Access Token bên trái để nạp Page.</td></tr>';
                return;
            }

            cachedPages.forEach(p => {
                let badgeToken = '<span class="badge bg-success">Sẵn sàng (OK)</span>';
                if (p.token_status === 'EXPIRED') badgeToken = '<span class="badge bg-danger">Hết hạn / Mất quyền</span>';
                else if (p.token_status === 'MISSING_PERMISSIONS') badgeToken = '<span class="badge bg-warning text-dark">Thiếu quyền Page</span>';
                else if (p.token_status === 'EXPIRING_SOON') badgeToken = '<span class="badge bg-warning text-dark">Sắp hết hạn</span>';

                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td><strong>${escapeHtml(p.page_name)}</strong><br><small class="text-muted">ID: ${escapeHtml(p.page_id)}</small></td>
                    <td><span class="badge bg-light text-dark border">${escapeHtml(p.account_name || 'N/A')}</span></td>
                    <td>${badgeToken}<br><small class="text-muted">${escapeHtml(p.status_note || '')}</small></td>
                    <td>${p.rule_enabled ? `<span class="badge bg-primary">Chờ ${p.delay_minutes}p (Tối đa ${p.max_comments_per_day}/ngày)</span>` : '<span class="badge bg-secondary">Tắt</span>'}</td>
                `;
                tbody.appendChild(tr);

                ruleSelect.innerHTML += `<option value="${escapeHtml(p.page_id)}">${escapeHtml(p.page_name)} (${escapeHtml(p.page_id)})</option>`;
                manualSelect.innerHTML += `<option value="${escapeHtml(p.page_id)}">${escapeHtml(p.page_name)}</option>`;
                filterPage.innerHTML += `<option value="${escapeHtml(p.page_id)}">${escapeHtml(p.page_name)}</option>`;
            });

            loadPageRule();
            loadRecentPostsForManual();
        }

        async function connectAccount() {
            const name = document.getElementById('accName').value.trim();
            const token = document.getElementById('accToken').value.trim();
            if (!name || !token) { alert('Vui lòng nhập tên nick và User Access Token!'); return; }

            const btn = document.getElementById('btnConnectAcc');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Đang xác thực...';

            try {
                const res = await fetch('/api/accounts/connect', {
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
                const res = await fetch('/api/sheets/sync', {
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
            document.getElementById('ruleEnabled').checked = page.rule_enabled == 1;
            try {
                const arr = JSON.parse(page.templates || '[]');
                document.getElementById('ruleTemplates').value = arr.join('\\n');
            } catch(e) {
                document.getElementById('ruleTemplates').value = page.templates || '';
            }
        }

        async function savePageRule() {
            const pageId = document.getElementById('rulePageSelect').value;
            const delay = parseInt(document.getElementById('ruleDelay').value) || 10;
            const maxPerDay = parseInt(document.getElementById('ruleMaxPerDay').value) || 20;
            const isEnabled = document.getElementById('ruleEnabled').checked;
            const templates = document.getElementById('ruleTemplates').value.split('\\n').map(t => t.trim()).filter(t => t.length > 0);

            if (templates.length === 0) { alert('Vui lòng nhập ít nhất 1 mẫu bình luận!'); return; }

            const res = await fetch(`/api/pages/${pageId}/rule`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ delay_minutes: delay, templates: templates, max_comments_per_day: maxPerDay, is_enabled: isEnabled })
            });
            const data = await res.json();
            if (res.ok) {
                alert('Đã lưu cấu hình kịch bản!');
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
                const res = await fetch(`/api/pages/${pageId}/recent_posts`);
                const data = await res.json();
                const posts = data.posts || [];
                if (posts.length === 0) {
                    container.innerHTML = '<small class="text-muted">Không có bài viết gần đây.</small>';
                    return;
                }
                container.innerHTML = '';
                posts.forEach(pt => {
                    const d = document.createElement('div');
                    d.className = 'post-card';
                    const text = pt.message ? pt.message.substring(0, 90) + '...' : '(Bài viết hình ảnh/video)';
                    const time = new Date(pt.created_time).toLocaleString('vi-VN');
                    d.innerHTML = `<div><strong>${escapeHtml(text)}</strong></div><small class="text-muted">Đăng lúc: ${escapeHtml(time)} | ID: <code>${escapeHtml(pt.id)}</code></small>`;
                    d.onclick = () => {
                        document.querySelectorAll('.post-card').forEach(el => el.classList.remove('selected'));
                        d.classList.add('selected');
                        document.getElementById('manualPostId').value = pt.id;
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
            const res = await fetch('/api/spintax/preview', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content })
            });
            const data = await res.json();
            box.style.display = 'block';
            if (res.ok) {
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

            let schedIso = null;
            if (!isNow) {
                if (!schedTime) { alert('Vui lòng chọn thời gian hẹn giờ!'); return; }
                schedIso = new Date(schedTime).toISOString();
            }

            const btn = document.getElementById('btnSubmitManual');
            btn.disabled = true;

            try {
                const res = await fetch('/api/tasks/manual', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        page_id: pageId,
                        post_id_or_url: postId,
                        content: content,
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

            const res = await fetch(url);
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
                const dateStr = new Date(t.scheduled_at).toLocaleString('vi-VN');
                const tr = document.createElement('tr');
                let actionBtns = '';

                if (t.status === 'NEED_CHECK') {
                    actionBtns = `
                        <button class="btn btn-sm btn-success me-1" onclick="resolveTask(${t.id})" title="Đã đối chiếu trên Facebook thấy có bình luận"><i class="fa-solid fa-check"></i> Đã đăng</button>
                        <button class="btn btn-sm btn-outline-primary me-1" onclick="retryTask(${t.id})" title="Gửi lại"><i class="fa-solid fa-rotate-right"></i> Gửi lại</button>
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
                    <td><strong>${escapeHtml(t.page_name)}</strong><br><small class="text-muted">${t.source === 'AUTO' ? '<i class="fa-solid fa-robot"></i> Tự động' : '<i class="fa-solid fa-user"></i> Thủ công'}</small></td>
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
            const res = await fetch(`/api/tasks/${id}/cancel`, { method: 'POST' });
            if (!res.ok) {
                const data = await res.json();
                alert(data.detail || 'Không thể hủy tác vụ');
            }
            loadTasks();
            fetchMetrics();
        }

        async function retryTask(id) {
            const res = await fetch(`/api/tasks/${id}/retry`, { method: 'POST' });
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
            const res = await fetch(`/api/tasks/${id}/resolve`, {
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
            fetchMetrics();
            fetchPages();
            loadTasks();
            setInterval(fetchMetrics, 10000);
        };
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)
