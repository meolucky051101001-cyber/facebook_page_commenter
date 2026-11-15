import os
import sys
import json
import time
import uuid
import asyncio
import datetime
import threading
import secrets
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any, Set
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Add root folder to sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antidetect.models import Profile, FingerprintConfig, ProxyConfig, REDACTED_SECRET
from antidetect.storage import (
    ProfileManager,
    ProfileBusyError,
    ProfileDeletionError,
    DATA_DIR,
    PROFILES_DATA_DIR,
)
from antidetect.profile_copy import (
    ProfileCopyError,
    new_operation_id,
    validate_idempotency_key,
)
from antidetect.fingerprint import (
    generate_fingerprint,
    get_host_device_memory,
    locale_for_country,
    detect_browser_brand,
)
from antidetect.profile_audit import audit_profiles, build_fix_plan
from antidetect.launch_gate import (
    evaluate_profile as _evaluate_launch_gate,
    evaluate_profiles as _evaluate_launch_gates,
    get_mode as _launch_gate_mode,
    set_mode as _set_launch_gate_mode,
    settings_snapshot as _launch_gate_settings,
)
from antidetect.launcher import BrowserLauncher
from antidetect.cdp_health import CdpUnavailable
from antidetect.fanpage_scan import (
    DISCOVERY_ENDPOINT_TIMEOUT_S,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_UNKNOWN,
    ensure_scan_contract,
    scan_defaults,
    scan_detail,
)
from antidetect.facebook import ScanAborted
from antidetect.profile_locks import ProfileLockTimeout
from antidetect.proxy import check_proxy
from antidetect.scheduler import ScheduleStoreCorruption
from antidetect.bulk_jobs import (
    BulkJobHeartbeat,
    BulkJobStore,
    BulkJobStoreCorruption,
)
from antidetect.applog import get_logger, setup_logging
from antidetect import metrics
from antidetect.fanpages import FanpageRevisionConflictError, FanpageService

from .schemas import (
    AuditFixRequest,
    BulkCreateRequest,
    CdpHealth,
    CheckProxyRequest,
    CreateProfileRequest,
    FanpageAlertUpdateRequest,
    FanpageBulkUpdateRequest,
    FanpageMergeRequest,
    FanpageOpenRequest,
    FanpagePruneRequest,
    FanpageSyncJobCreateRequest,
    FanpageSyncJobRetryRequest,
    FanpageUpdateRequest,
    ImportCookiesRequest,
    ImportProfilesRequest,
    LaunchGateSettingsRequest,
    ProfileRuntime,
    ScheduleCreateRequest,
    ScheduleUpdateRequest,
    StartAllBody,
    StopAllBody,
    SwitchLocationRequest,
    UpdateProfileRequest,
)
from .services import (
    apply_geo_to_profile as _apply_geo_to_profile,
    auto_max_concurrent as _auto_max_concurrent,
    enrich_facebook_pages as _enrich_facebook_pages,
    fanpage_stale_after_hours as _fanpage_stale_after_hours,
    host_gpu_strings as _host_gpu_strings,
    profile_display_timezone as _profile_display_timezone,
    ram_by_profile_dir as _ram_by_profile_dir,
    sanitize_facebook_pages as _sanitize_facebook_pages,
    validate_iana_timezone,
    worker_pool as _worker_pool,
)

log = get_logger("web")


def _profiles_with_status():
    """List profiles + sync trạng thái running; chạy trong thread.

    is_running có thể spawn PowerShell (block tới 5s/lần) — gọi trực tiếp trên
    event loop sau khi restart server với nhiều browser đang mở sẽ đóng băng
    toàn bộ API (kể cả /health, /stop).
    """
    profiles = profile_manager.list_profiles()
    runtime_profiles = []
    running_dirs: Dict[str, str] = {}
    for p in profiles:
        if launcher.is_running(p.id):
            p.status = "running"
            p.port = launcher.active_ports.get(p.id)
            running_dirs[p.id] = os.path.join(PROFILES_DATA_DIR, p.id, "user_data")
    ram_map = _ram_by_profile_dir(running_dirs) if running_dirs else {}
    for p in profiles:
        item = ProfileRuntime(**p.model_dump())
        if p.status == "running":
            record = launcher._runtime_store.get(p.id) or {}
            started = record.get("started_at")
            if started:
                item.uptime_seconds = max(0, int(time.time()) - int(started))
            item.ram_mb = round(ram_map.get(p.id, 0.0), 1) or None
            item.cdp_health = CdpHealth(
                **launcher.cdp_health_snapshot(p.id, running=True)
            )
        runtime_profiles.append(item)
    return runtime_profiles


_CDP_CONFLICT_CODES = {
    "CDP_UNHEALTHY",
    "CDP_STARTING",
    "RECOVERY_REQUIRED",
    "DETACHED_NO_CDP",
    "PROFILE_BUSY",
}


def _cdp_conflict_response(exc: CdpUnavailable) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "success": False,
            "code": exc.code,
            "detail": str(exc),
            "cdp_health": exc.health,
        },
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging(os.path.join(DATA_DIR, "app.log"), console=False)
    # Quy mô ~20 profile: mỗi lệnh start giữ một worker thread suốt handshake
    # CDP (~10-15s). Pool mặc định của loop chỉ có min(32, cpu+4) thread, nên
    # stop/list gửi ngay sau batch start sẽ xếp hàng — nâng lên 64.
    try:
        asyncio.get_running_loop().set_default_executor(_worker_pool())
    except Exception:
        pass
    # run.py needs the per-start key to open the UI with /#key. The record is
    # protected by the current Windows user's DPAPI.
    try:
        from antidetect.runtime_auth import write_runtime

        write_runtime(
            {
                "access_key": app.state.access_key,
                "session_key": app.state.session_key,
                "instance_id": app.state.instance_id,
            }
        )
    except Exception as e:
        log.warning("Không ghi được runtime key: %s", e)
    # Đợt C: lịch tự động — runner daemon dậy mỗi 30s.
    global _scheduler, _schedule_store
    try:
        from antidetect.scheduler import ScheduleStore, Scheduler

        _schedule_store = ScheduleStore(os.path.join(DATA_DIR, "schedules.json"))
        _scheduler = Scheduler(
            _schedule_store,
            callbacks={
                "start_all": _scheduler_start_all,
                "stop_all": launcher.stop_all,
                "sync_location_proxy": _scheduler_sync_all_locations,
                "sync_fanpages": _scheduler_sync_fanpages,
                "sync_fanpages_deep": _scheduler_sync_fanpages_deep,
            },
            history_path=os.path.join(DATA_DIR, "schedule_runs.json"),
        )
        _scheduler.start()
    except Exception as e:
        log.warning("Không khởi động được scheduler: %s", e)
    yield
    try:
        if _scheduler is not None:
            _scheduler.stop()
    except Exception:
        pass
    # Đóng toàn bộ trình duyệt đang chạy khi tắt server.
    launcher.stop_all()
    try:
        from antidetect.runtime_auth import delete_runtime

        delete_runtime()
    except Exception:
        pass


_scheduler = None
_schedule_store = None

app = FastAPI(
    title="Antidetect Python API",
    description="REST API & Dashboard cho Antidetect Browser",
    version="1.1.0",
    lifespan=lifespan,
)
app.state.access_key = secrets.token_urlsafe(32)
app.state.session_key = secrets.token_urlsafe(32)
app.state.instance_id = secrets.token_hex(16)

# P0-06: Kho lưu trữ mã mở khóa một lần (One-Time Unlock Codes) với TTL 60 giây
_one_time_codes: Dict[str, float] = {}
_otc_lock = threading.Lock()


def create_unlock_code(ttl_seconds: int = 60) -> str:
    """Tạo mã mở khóa dùng 1 lần (OTC) có hiệu lực ngắn (mặc định 60s)."""
    now = time.time()
    code = f"otc_{secrets.token_urlsafe(32)}"
    with _otc_lock:
        expired = [k for k, exp in _one_time_codes.items() if exp < now]
        for k in expired:
            _one_time_codes.pop(k, None)
        _one_time_codes[code] = now + ttl_seconds
    return code


def consume_unlock_code(code: str) -> bool:
    """Kiểm tra và hủy ngay lập tức mã mở khóa nếu còn hạn (chống replay attack)."""
    now = time.time()
    with _otc_lock:
        exp = _one_time_codes.pop(code, None)
        if exp is not None and exp >= now:
            return True
    return False


def authorized(request):
    bearer = request.headers.get('authorization', '') or ''
    cookie = request.cookies.get('antidetect_session', '') or ''
    # Header decode latin-1 có thể chứa ký tự non-ASCII; encode về bytes để
    # compare_digest không ném TypeError (vốn thành lỗi 500) trên input lạ.
    key = ('Bearer ' + app.state.access_key).encode('utf-8', 'replace')
    session = app.state.session_key.encode('utf-8', 'replace')
    if bearer and secrets.compare_digest(bearer.encode('utf-8', 'replace'), key):
        return True
    if cookie and secrets.compare_digest(cookie.encode('utf-8', 'replace'), session):
        return True
    # Đợt C: API token dài hạn cho automation — so SHA-256(bearer) với hash
    # lưu DPAPI. Plaintext token không bao giờ nằm trên đĩa.
    if bearer.startswith('Bearer '):
        try:
            from antidetect.runtime_auth import read_api_token_hash
            stored = read_api_token_hash()
            if stored:
                import hashlib as _hashlib
                candidate = _hashlib.sha256(
                    bearer[7:].strip().encode('utf-8')
                ).hexdigest()
                if secrets.compare_digest(candidate, stored):
                    return True
        except Exception:
            pass
    return False


# Dashboard và API dùng cùng origin; không bật CORS cho các cổng localhost khác.
@app.middleware("http")
async def local_origin_check_middleware(request: Request, call_next):
    if request.url.hostname not in ("localhost", "127.0.0.1"):
        return JSONResponse(status_code=403, content={"error": "Host không hợp lệ."})
    path = request.url.path
    if path.startswith(("/api/", "/auth/")):
        from urllib.parse import urlparse

        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        sec_fetch_site = request.headers.get("sec-fetch-site")

        if sec_fetch_site == "cross-site":
            return JSONResponse(status_code=403, content={"error": "Request cross-site bị từ chối."})

        expected_origin = f"{request.url.scheme}://{request.url.netloc}".lower()
        if origin:
            parsed = urlparse(origin)
            actual_origin = f"{parsed.scheme}://{parsed.netloc}".lower()
            if actual_origin != expected_origin:
                return JSONResponse(status_code=403, content={"error": "Origin không khớp dashboard."})
        elif referer:
            parsed = urlparse(referer)
            actual_origin = f"{parsed.scheme}://{parsed.netloc}".lower()
            if actual_origin != expected_origin:
                return JSONResponse(status_code=403, content={"error": "Referer không khớp dashboard."})
        elif sec_fetch_site in ("cross-site", "same-site"):
            return JSONResponse(status_code=403, content={"error": "Request trình duyệt không có origin hợp lệ."})
    # /docs, /redoc và /openapi.json phơi toàn bộ API surface; yêu cầu cùng
    # phiên hợp lệ như /api/ thay vì để mở trên localhost.
    needs_auth = path.startswith('/api/') or path in (
        '/docs', '/redoc', '/openapi.json'
    ) or path.startswith(('/docs/', '/redoc/'))
    if needs_auth and not authorized(request):
        return JSONResponse(status_code=401, content={"detail": "Phiên truy cập hết hạn. Hãy mở lại ứng dụng từ shortcut."})
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    # P2-04: Content Security Policy chống XSS và script injection.
    # Inline <script> đã chuyển ra /static (P1-8); chỉ còn inline handler
    # (onclick=) nên script-src-attr vẫn cần 'unsafe-inline'. Các directive
    # dưới đây chặn plugin, base tag và form gửi ra ngoài.
    # Swagger UI/ReDoc mặc định tải bundle từ jsdelivr và font từ Google
    # Fonts; chỉ nới CSP trên đúng trang docs (đã yêu cầu phiên), dashboard
    # vẫn giữ CSP siết.
    if path in ('/docs', '/redoc') or path.startswith(('/docs/', '/redoc/')):
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
            "https://fonts.googleapis.com; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self' https:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none';"
        )
    else:
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "script-src-attr 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; "
            "img-src 'self' data: https:; "
            "connect-src 'self' https:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none';"
        )
    return response

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
_static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

profile_manager = ProfileManager()
launcher = BrowserLauncher(profile_manager)

# P1-06: Khóa và theo dõi trạng thái hàng đợi Start-All
_bulk_launch_lock = threading.Lock()
_start_all_lock = threading.Lock()
_active_start_all_job: Optional[Dict[str, Any]] = None
_start_all_cancel_event: Optional[threading.Event] = None
_bulk_job_store = BulkJobStore(os.path.join(DATA_DIR, "bulk_jobs.json"))


def _record_bulk_progress(
    job_id: str, profile_id: str, res: Dict[str, Any]
) -> None:
    """Cập nhật job cục bộ + file trạng thái dùng chung; lỗi ghi chỉ log."""
    with _start_all_lock:
        job = _active_start_all_job
        if not job or job.get("job_id") != job_id:
            return
        health_state = (res.get("cdp_health") or {}).get("state")
        cdp_failed = res.get("success") and (
            res.get("cdp_ready") is False
            or (health_state and health_state != "healthy")
        )
        if res.get("success") and not cdp_failed:
            job["completed_count"] += 1
        else:
            job["failed_count"] += 1
        job["results"].append({"profile_id": profile_id, "result": res})
        completed = job["completed_count"]
        failed = job["failed_count"]
        results = list(job["results"])
    try:
        _bulk_job_store.update(
            job_id,
            app.state.instance_id,
            completed_count=completed,
            failed_count=failed,
            results=results,
        )
    except Exception as exc:  # noqa: BLE001 - progress chỉ là quan sát
        log.warning("Không ghi được tiến độ bulk job: %s", exc)


def _finish_bulk_job(
    job_id: str, status: str, error: Optional[str] = None
) -> None:
    with _start_all_lock:
        job = _active_start_all_job
        snapshot = None
        if job and job.get("job_id") == job_id:
            job["status"] = status
            job["end_time"] = time.time()
            if error:
                job["error"] = error
            snapshot = dict(job)
    if snapshot is None:
        return
    try:
        _bulk_job_store.finish(
            job_id,
            app.state.instance_id,
            status,
            error=error,
            results=snapshot.get("results"),
        )
    except Exception as exc:  # noqa: BLE001 - job đã chạy xong
        log.warning("Không ghi được trạng thái bulk job: %s", exc)


# --- Web Page Route ---
@app.get('/health')
async def health():
    return {'application': 'antidetect-python', 'instance_id': app.state.instance_id}


@app.get('/api/system/metrics')
async def system_metrics():
    """Counters/timings của tiến trình hiện tại + lịch sử lịch gần nhất."""
    data = metrics.snapshot()
    data["scheduler"] = {
        "last_tick": (
            _scheduler.last_run_info.get("last_tick") if _scheduler else None
        ),
        "recent_runs": _scheduler.run_history.recent(10) if _scheduler else [],
    }
    return data


@app.get('/unlock', response_class=HTMLResponse)
async def unlock(request: Request):
    return templates.TemplateResponse(request=request, name='unlock.html', context={})


@app.post('/auth/login')
async def login(request: Request):
    auth_header = request.headers.get('authorization', '')
    token = auth_header[7:].strip() if auth_header.startswith('Bearer ') else auth_header.strip()

    is_valid = False
    if token:
        if token.startswith("otc_"):
            # P0-06: Xác thực mã dùng một lần và tiêu hủy ngay
            is_valid = consume_unlock_code(token)
        elif secrets.compare_digest(
            token.encode('utf-8', 'replace'),
            app.state.access_key.encode('utf-8', 'replace'),
        ):
            # Cho phép access_key cố định qua API trực tiếp hoặc test suite.
            # So sánh trên bytes để input non-ASCII trả 401 thay vì TypeError 500.
            is_valid = True

    if not is_valid:
        raise HTTPException(status_code=401, detail='Khóa mở ứng dụng không hợp lệ hoặc đã hết hạn')

    response = JSONResponse({'success': True})
    response.set_cookie('antidetect_session', app.state.session_key, httponly=True, samesite='strict', path='/')
    return response


@app.post('/auth/create-unlock-code')
async def create_unlock_code_endpoint(request: Request):
    if not authorized(request):
        raise HTTPException(status_code=401, detail='Chưa xác thực')
    code = create_unlock_code(ttl_seconds=60)
    return {"unlock_code": code, "expires_in": 60}


@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    if not authorized(request):
        return templates.TemplateResponse(
            request=request, name="unlock.html", context={}
        )
    profiles = await asyncio.to_thread(_profiles_with_status)
    return templates.TemplateResponse(
        request=request, name="index.html", context={"profiles": profiles}
    )



def _commenter_request(method, path, body=None):
    """Proxy fixed Commenter routes using the local dashboard key."""
    import re
    from pathlib import Path
    import requests

    default_env = r"C:\Projects\Tools\facebook comment\facebook_page_commenter\.env"
    env_path = Path(os.environ.get("FACEBOOK_COMMENTER_ENV", default_env))
    port = os.environ.get("FACEBOOK_COMMENTER_PORT", "8080")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("Invalid Commenter port")
    values = re.findall(r"(?m)^DASHBOARD_ADMIN_KEY\s*=\s*([A-Za-z0-9_-]{32,})\s*$", env_path.read_text(encoding="utf-8-sig"))
    if len(values) != 1:
        raise ValueError("Commenter key is missing or ambiguous")
    with requests.Session() as client:
        client.trust_env = False
        client.headers["X-Dashboard-Key"] = values[0]
        response = client.request(
            method, f"http://127.0.0.1:{port}{path}", json=body,
            timeout=(3, 80 if method == "POST" else 5), allow_redirects=False,
        )
        return response.status_code, response.json()


@app.api_route("/api/commenter/pages/{page_id}/{action}", methods=["GET", "POST"])
async def commenter_page_action(request: Request, page_id: str, action: str):
    import re
    import requests

    methods = {"preview": "GET", "check": "POST", "automation": "POST", "reactivate": "POST"}
    if not re.fullmatch(r"[0-9]{5,30}", page_id) or methods.get(action) != request.method:
        raise HTTPException(status_code=404, detail="Thao tác Page không hợp lệ.")
    body = None
    if action == "automation":
        try:
            data = await request.json()
        except ValueError:
            data = None
        if not isinstance(data, dict) or type(data.get("enabled")) is not bool:
            raise HTTPException(status_code=400, detail="Cần chọn bật hoặc tạm dừng.")
        body = {"enabled": data["enabled"]}
    try:
        status, data = await asyncio.to_thread(
            _commenter_request, request.method, f"/api/pages/{page_id}/{action}", body,
        )
    except (OSError, ValueError, requests.RequestException):
        raise HTTPException(status_code=502, detail="Không kết nối được Commenter cục bộ.") from None
    return JSONResponse(data, status_code=status)


@app.post("/api/commenter/sheets/sync")
async def commenter_sync_sheet(request: Request):
    from urllib.parse import urlparse
    import requests

    try:
        payload = await request.json()
    except ValueError:
        payload = None
    raw_url = payload.get("sheet_url") if isinstance(payload, dict) else None
    sheet_url = raw_url.strip() if isinstance(raw_url, str) else ""
    parsed = urlparse(sheet_url)
    if (
        not sheet_url
        or len(sheet_url) > 2048
        or parsed.scheme != "https"
        or parsed.netloc != "docs.google.com"
        or not parsed.path.startswith("/spreadsheets/d/")
    ):
        raise HTTPException(status_code=400, detail="Hãy nhập đường dẫn Google Sheet hợp lệ.")

    try:
        status, data = await asyncio.to_thread(
            _commenter_request, "POST", "/api/sheets/sync", {"sheet_url": sheet_url},
        )
    except (OSError, ValueError, requests.RequestException):
        raise HTTPException(status_code=502, detail="Không đồng bộ được Google Sheet từ Commenter cục bộ.") from None
    return JSONResponse(data, status_code=status)


@app.get("/commenter/actions.js")
async def commenter_actions_script():
    from fastapi.responses import Response

    return Response("""
        const output = document.getElementById('action-result');
        document.querySelectorAll('button[data-action]').forEach(button => {
            button.addEventListener('click', async () => {
                const {page, action, enable} = button.dataset;
                button.disabled = true;
                output.hidden = false;
                output.textContent = 'Đang xử lý…';
                try {
                    const response = await fetch('/api/commenter/pages/' + encodeURIComponent(page) + '/' + action, {
                        method: action === 'preview' ? 'GET' : 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: action === 'automation' ? JSON.stringify({enabled: enable === '1'}) : undefined,
                    });
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.detail || 'Không thực hiện được thao tác.');
                    if (action === 'preview') {
                        const sequence = data.comment_sequence || [];
                        const lines = sequence.length
                            ? sequence.map((step, index) => '#' + (index + 1) + ' · Sau ' + step.delay_minutes + ' phút · ' + step.template)
                            : ['Thời gian chờ: ' + data.delay_minutes + ' phút', ...(data.templates || [])];
                        output.textContent = [data.source_error, ...lines].filter(Boolean).join(String.fromCharCode(10, 10));
                    } else {
                        sessionStorage.setItem('commenter-result', data.message ||
                            (data.enabled ? 'Đã bật tự động cho Page.' : 'Đã tạm dừng tự động cho Page.'));
                        location.reload();
                    }
                } catch (error) {
                    output.textContent = error.message;
                } finally {
                    button.disabled = false;
                }
            });
        });
        const previous = sessionStorage.getItem('commenter-result');
        if (previous) {
            sessionStorage.removeItem('commenter-result');
            output.hidden = false;
            output.textContent = previous;
        }

        const sheetForm = document.getElementById('commenter-sheet-form');
        if (sheetForm) {
            sheetForm.addEventListener('submit', async event => {
                event.preventDefault();
                const button = document.getElementById('commenter-sheet-submit');
                const sheetUrl = document.getElementById('commenter-sheet-url').value.trim();
                button.disabled = true;
                output.hidden = false;
                output.textContent = 'Đang đồng bộ cấu hình Sheet…';
                try {
                    const response = await fetch('/api/commenter/sheets/sync', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({sheet_url: sheetUrl}),
                    });
                    const data = await response.json();
                    if (!response.ok || !data.success) {
                        throw new Error(data.detail || data.error || 'Không đồng bộ được Sheet.');
                    }
                    const errors = (data.errors || []).slice(0, 8);
                    sessionStorage.setItem('commenter-result',
                        [data.message || 'Đã đồng bộ Sheet.', ...errors].join(String.fromCharCode(10)));
                    location.reload();
                } catch (error) {
                    output.textContent = error.message;
                } finally {
                    button.disabled = false;
                }
            });
        }
    """, media_type="application/javascript")


@app.get("/commenter/", response_class=HTMLResponse)
async def commenter_overview(request: Request):
    if not authorized(request):
        return templates.TemplateResponse(request=request, name="unlock.html", context={}, status_code=401)

    from html import escape
    from itertools import groupby
    import requests

    def label(value):
        return escape(str(value or ""))

    def profile_key(page):
        return (page.get("antidetect_profile_name") or "Chưa ghép profile",
                page.get("antidetect_profile_id") or "")

    def button(page, action, title, enabled=True):
        page_id = label(page["page_id"])
        enable = "0" if page.get("rule_enabled") else "1"
        disabled = "" if enabled else " disabled"
        return f'<button type="button" data-page="{page_id}" data-action="{action}" data-enable="{enable}"{disabled}>{title}</button>'

    try:
        (metrics_status, overview), (pages_status, catalog) = await asyncio.gather(
            asyncio.to_thread(_commenter_request, "GET", "/api/metrics"),
            asyncio.to_thread(_commenter_request, "GET", "/api/pages"),
        )
        if metrics_status != 200 or pages_status != 200:
            raise ValueError("Commenter did not return dashboard data")
        metrics = overview["metrics"]
        sheet = metrics["sheet_sync"]
        state = "Tạm dừng" if metrics["paused_all"] else "Đang chạy"
        counts = overview["task_counts"]
        details = [
            f'<p>Hệ thống: <strong>{state}</strong> · Chờ: {int(counts["PENDING"])}'
            f' · Đang gửi: {int(counts["SENDING"])} · Cần kiểm tra: {int(counts["NEED_CHECK"])}</p>',
            f'<p>Nguồn mẫu hiện tại: {label(sheet.get("sheet_url")) or "Chưa cấu hình"}<br>{label(sheet.get("last_error"))}</p>',
            '<section class="sheet-panel"><h2>Quản lý Sheet bình luận</h2>'
            '<p>Dán URL của tab <strong>Comments - Multi-Page</strong> rồi đồng bộ. Mỗi dòng cấu hình một mẫu cho Page bằng ID, tên Page, nội dung comment và số phút chờ.</p>'
            '<form id="commenter-sheet-form">'
            '<label for="commenter-sheet-url">Google Sheet URL</label>'
            f'<input id="commenter-sheet-url" name="sheet_url" type="url" maxlength="2048" required value="{label(sheet.get("sheet_url"))}" placeholder="https://docs.google.com/spreadsheets/d/...">'
            '<button id="commenter-sheet-submit" type="submit">Lưu và đồng bộ Sheet</button>'
            '</form><small>Page chỉ chạy tự động khi có mẫu và thời gian chờ hợp lệ, kết nối Meta đã xác minh và được bật trong danh sách bên dưới.</small></section>',
        ]
        for (profile, profile_id), pages in groupby(sorted(catalog["pages"], key=profile_key), key=profile_key):
            details.append(f'<h2>{label(profile)} <small>{label(profile_id)}</small></h2>')
            details.append('<div class="scroll"><table><thead><tr><th>Fanpage</th><th>Meta</th>'
                           '<th>Tự động / chờ</th><th>Lỗi gần nhất</th><th>Thao tác</th></tr></thead><tbody>')
            for page in pages:
                actions = button(page, "preview", "Xem mẫu")
                actions += button(page, "check", "Kiểm tra Meta", page.get("has_page_token"))
                if page.get("has_page_token") and not page.get("is_active"):
                    actions += button(page, "reactivate", "Kích hoạt kết nối")
                enabled = page.get("rule_enabled")
                actions += button(page, "automation", "Tạm dừng" if enabled else "Bật tự động",
                                  enabled or (page.get("has_page_token") and page.get("is_active")))
                verified = "Đã xác minh" if page.get("graph_ready") else "Cần kiểm tra"
                delay = page.get("delay_minutes")
                errors = [page.get(key) for key in ("connection_error", "scan_error", "source_error", "send_error", "send_wait_reason")]
                errors = "<br>".join(label(error) for error in dict.fromkeys(errors) if error)
                details.append(f'<tr><th scope="row">{label(page["page_name"])}<br><small>{label(page["page_id"])}</small></th>'
                               f'<td>{verified}<br>{label(page.get("token_status"))}</td>'
                               f'<td>{"Đã bật" if enabled else "Tạm dừng"} · {label(delay)} phút<br>'
                               f'Đã dùng {int(page.get("comments_used_today", 0))}/{int(page.get("max_comments_per_day") or 20)}, '
                               f'còn {int(page.get("comments_remaining_today", 0))}</td>'
                               f'<td>{errors or "—"}</td><td>{actions}</td></tr>')
            details.append('</tbody></table></div>')
        if not catalog["pages"]:
            details.append("<p>Chưa có Fanpage trong danh mục.</p>")
    except (OSError, ValueError, KeyError, TypeError, requests.RequestException):
        details = ["<p>Chưa kết nối được Commenter. Kiểm tra dịch vụ cục bộ và cổng được cấu hình.</p>"]

    return HTMLResponse(
        '<!doctype html><html lang="vi"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Bình luận Fanpage</title><style>'
        'body{font:16px system-ui,sans-serif;background:#101828;color:#f8fafc;margin:0}'
        'main{max-width:1200px;margin:40px auto;padding:0 20px}a{color:#93c5fd}nav{display:flex;gap:20px}'
        '.scroll{overflow:auto}table{width:100%;border-collapse:collapse;text-align:left}'
        'th,td{padding:10px;border-bottom:1px solid #344054;vertical-align:top}'
        'small{color:#b8c5d6}.sheet-panel{margin:20px 0;padding:18px;border:1px solid #344054;border-radius:12px;background:#182230}'
        '.sheet-panel form{display:flex;gap:10px;align-items:end;flex-wrap:wrap}.sheet-panel label{display:block;width:100%}'
        '.sheet-panel input{flex:1;min-width:280px;padding:9px;border:1px solid #526174;border-radius:6px;background:#101828;color:#f8fafc}'
        'button{margin:2px;padding:7px 10px;cursor:pointer}button:disabled{opacity:.6;cursor:wait}pre{white-space:pre-wrap}'
        '</style><script src="/commenter/actions.js" defer></script></head><body><main>'
        '<nav><a href="/">Profiles</a><a href="/fanpages">Fanpage Manager</a>'
        '<a href="/commenter/">Làm mới</a><a href="http://127.0.0.1:8080/operations">Hộp thư, xuất bản &amp; báo cáo</a></nav><h1>Bình luận Fanpage</h1>'
        '<pre id="action-result" role="status" hidden></pre>'
        + "".join(details) + '</main></body></html>'
    )


# --- REST API Endpoints ---
@app.get("/api/profiles", response_model=List[ProfileRuntime])
async def list_profiles():
    return await asyncio.to_thread(_profiles_with_status)


@app.post("/api/profiles", response_model=Profile)
async def create_profile(req: CreateProfileRequest):
    # Nếu chưa truyền fingerprint, tự sinh theo OS, vị trí và proxy
    fp = req.fingerprint
    proxy = req.proxy or ProxyConfig()

    if fp is None:
        from antidetect.fingerprint import get_location_coordinates
        auto_lat, auto_lon, auto_tz = get_location_coordinates(req.city or "hanoi", randomize=True)
        timezone = auto_tz
        locale = "vi-VN"
        lat, lon = auto_lat, auto_lon

        # Nếu có proxy và bật, thử lấy geoip để đồng bộ múi giờ, locale & tọa độ theo proxy
        if proxy.enabled and proxy.host and proxy.port:
            geo = await asyncio.to_thread(check_proxy, proxy, 5)
            if geo.get("success"):
                timezone = geo.get("timezone", timezone)
                locale = locale_for_country(geo.get("country_code"), default=locale)
                lat = geo.get("lat", lat)
                lon = geo.get("lon", lon)

        fp = generate_fingerprint(
            target_os=req.os,
            timezone=timezone,
            locale=locale,
            latitude=lat,
            longitude=lon,
            city=req.city or "hanoi",
            browser_brand=detect_browser_brand(getattr(launcher, "browser_path", None)),
        )

    profile = profile_manager.create_profile(
        name=req.name,
        target_os=req.os,
        proxy=proxy,
        fingerprint=fp,
        notes=req.notes or "",
        tags=req.tags or []
    )
    return profile


@app.post("/api/profiles/start-all")
async def start_all_profiles(
    url: Optional[str] = None,
    max_concurrent: Optional[int] = None,
    body: Optional[StartAllBody] = None,
):
    if url:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="URL không hợp lệ: chỉ chấp nhận http:// hoặc https://")
    if max_concurrent is None:
        # Chọn theo RAM máy (16GB → 6 Chrome đồng thời) — an toàn cho quy mô
        # ~20 profile; client vẫn override được bằng query param.
        max_concurrent = _auto_max_concurrent()
    max_concurrent = max(0, int(max_concurrent or 0))

    global _active_start_all_job, _start_all_cancel_event
    body_ids = body.profile_ids if body is not None else None
    if body_ids is not None and not body_ids:
        raise HTTPException(
            status_code=400,
            detail="Danh sách profile được chọn trống. Bỏ profile_ids để chạy toàn bộ.",
        )
    profiles = await asyncio.to_thread(_profiles_with_status)
    selected_ids = set(body_ids) if body_ids is not None else None
    if selected_ids is not None:
        profiles = [p for p in profiles if p.id in selected_ids]
        if not profiles:
            raise HTTPException(status_code=404, detail="Không profile nào trong danh sách chọn tồn tại.")

    to_start = [p for p in profiles if p.status != "running"]
    gate_mode = _launch_gate_mode()
    blocked = []
    allowed_to_start = to_start
    launch_ids: Optional[List[str]] = (
        list(selected_ids) if selected_ids is not None else None
    )
    if gate_mode != "off":
        browser_paths = await asyncio.to_thread(_resolve_browser_paths, profiles)
        gate_by_id = await asyncio.to_thread(
            _evaluate_launch_gates, profiles, browser_paths, gate_mode
        )
        blocked = [
            gate_by_id[p.id]
            for p in to_start
            if p.id in gate_by_id and not gate_by_id[p.id].allowed
        ]
        blocked_ids = {item.profile_id for item in blocked}
        allowed_to_start = [p for p in to_start if p.id not in blocked_ids]
        if to_start and not allowed_to_start:
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "detail": (
                        f"Cổng fingerprint chặn toàn bộ {len(blocked)} profile "
                        "trong danh sách. Sửa trong Health Center hoặc đổi chế độ cổng."
                    ),
                    "blocked": [item.as_dict() for item in blocked],
                    "gate_mode": gate_mode,
                },
            )
        if blocked_ids:
            base_ids = (
                list(selected_ids)
                if selected_ids is not None
                else [p.id for p in profiles]
            )
            launch_ids = [pid for pid in base_ids if pid not in blocked_ids]

    # Lock order: _bulk_launch_lock -> _start_all_lock -> bulk job IPC lock.
    # The bulk thread holds the process lock for its whole lifetime, so a
    # failure to acquire it means another local bulk launch is alive. A second
    # server instance is rejected by the file-backed job store instead.
    if not _bulk_launch_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="Một tác vụ Start-All khác đang chạy. Hãy đợi hoặc hủy tác vụ hiện tại."
        )
    try:
        with _start_all_lock:
            if _active_start_all_job is not None and _active_start_all_job.get("status") in ("running", "cancelling"):
                raise HTTPException(
                    status_code=409,
                    detail=f"Đang có tác vụ Start-All ({_active_start_all_job.get('job_id')}) đang xử lý. Hãy đợi hoặc hủy tác vụ hiện tại."
                )
            job_id = str(uuid.uuid4())
            cancel_event = threading.Event()
        try:
            claimed, existing = _bulk_job_store.try_claim(
                job_id=job_id,
                owner_instance=app.state.instance_id,
                kind="start_all",
                total=len(profiles),
                starting_count=len(allowed_to_start),
                max_concurrent=max_concurrent,
            )
        except BulkJobStoreCorruption as exc:
            log.error("Bulk job state hỏng: %s", exc)
            raise HTTPException(
                status_code=503,
                detail="Trạng thái Start-All dùng chung bị hỏng. Kiểm tra data/bulk_jobs.json trước khi chạy lại.",
            ) from exc
        except TimeoutError as exc:
            raise HTTPException(
                status_code=503,
                detail="Không lấy được khóa trạng thái Start-All; thử lại sau.",
            ) from exc
        if not claimed:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Đang có tác vụ {existing.get('kind') or 'Start-All'} "
                    f"({existing.get('job_id')}) do instance "
                    f"{str(existing.get('owner_instance') or 'khác')[:8]} xử lý. "
                    "Hãy đợi hoặc hủy tác vụ hiện tại."
                ),
            )
        with _start_all_lock:
            _start_all_cancel_event = cancel_event
            _active_start_all_job = {
                "job_id": job_id,
                "kind": "start_all",
                "status": "running",
                "owner_instance": app.state.instance_id,
                "total": len(profiles),
                "starting_count": len(allowed_to_start),
                "blocked_count": len(blocked),
                "gate_mode": gate_mode,
                "completed_count": 0,
                "failed_count": 0,
                "max_concurrent": max_concurrent,
                "results": [],
                "start_time": time.time(),
                "heartbeat_at": time.time(),
                "cancel_requested": False,
            }
    except BaseException:
        _bulk_launch_lock.release()
        raise

    def run_bulk():
        heartbeat = BulkJobHeartbeat(
            _bulk_job_store, job_id, app.state.instance_id, cancel_event
        )
        status = "completed"
        error = None
        try:
            heartbeat.start()
            launcher.launch_all(
                delay=0.8,
                open_url=url,
                max_concurrent=max_concurrent,
                cancel_event=cancel_event,
                progress_callback=on_progress,
                profile_ids=launch_ids,
            )
            if cancel_event.is_set():
                status = "cancelled"
        except Exception as exc:
            status = "failed"
            error = str(exc)[:500]
        finally:
            heartbeat.stop()
            _finish_bulk_job(job_id, status, error)
            _bulk_launch_lock.release()

    def on_progress(p_id, res):
        _record_bulk_progress(job_id, p_id, res)

    try:
        threading.Thread(target=run_bulk, daemon=True).start()
    except Exception as exc:
        _finish_bulk_job(job_id, "failed", str(exc)[:500])
        _bulk_launch_lock.release()
        raise HTTPException(status_code=500, detail="Không khởi động được luồng Start-All.") from exc

    limit_label = (
        "không giới hạn" if max_concurrent <= 0 else f"tối đa {max_concurrent}"
    )
    start_count = len(allowed_to_start) if gate_mode != "off" else len(to_start)
    message = f"Đang khởi chạy {start_count} profile ({limit_label} cùng lúc)..."
    if blocked:
        message += f" Bỏ qua {len(blocked)} profile bị cổng fingerprint chặn."
    return {
        "success": True,
        "message": message,
        "job_id": job_id,
        "total": len(profiles),
        "starting_count": start_count,
        "max_concurrent": max_concurrent,
        "blocked": [item.as_dict() for item in blocked],
        "gate_mode": gate_mode,
    }


@app.get("/api/profiles/start-all/status")
async def get_start_all_status():
    """P1-06/P2-12: tiến độ Start-All, kể cả job do instance khác sở hữu."""
    with _start_all_lock:
        local_job = dict(_active_start_all_job) if _active_start_all_job else None
    try:
        remote_job = _bulk_job_store.read()
    except BulkJobStoreCorruption as exc:
        log.error("Bulk job state hỏng: %s", exc)
        return {
            "active": False,
            "job": None,
            "error": "Trạng thái Start-All dùng chung bị hỏng; kiểm tra data/bulk_jobs.json.",
        }
    job = local_job
    if remote_job and (
        not local_job or remote_job.get("job_id") != local_job.get("job_id")
    ):
        job = remote_job
    if not job:
        return {"active": False, "job": None}
    job["remote"] = job.get("owner_instance") != app.state.instance_id
    if job is local_job:
        active = job.get("status") in ("running", "cancelling")
    else:
        active = _bulk_job_store.is_active(job)
    if not active and job.get("status") in ("running", "cancelling"):
        job["stale"] = True
    return {"active": active, "job": job}


@app.post("/api/profiles/start-all/cancel")
async def cancel_start_all():
    """P1-06/P2-12: hủy job cục bộ hoặc gửi yêu cầu tới instance đang sở hữu."""
    with _start_all_lock:
        local_job = _active_start_all_job
        local_active = bool(
            local_job and local_job.get("status") in ("running", "cancelling")
        )
        if local_active:
            job_id = local_job.get("job_id")
            if _start_all_cancel_event:
                _start_all_cancel_event.set()
            local_job["status"] = "cancelling"
    if local_active:
        try:
            _bulk_job_store.request_cancel(job_id)
        except Exception as exc:  # noqa: BLE001 - local cancel vẫn có hiệu lực
            log.warning("Không ghi được yêu cầu hủy bulk job: %s", exc)
        return {
            "success": True,
            "job_id": job_id,
            "remote": False,
            "message": "Đã gửi yêu cầu dừng hàng đợi Start-All.",
        }
    try:
        remote_job = _bulk_job_store.request_cancel()
    except BulkJobStoreCorruption as exc:
        log.error("Bulk job state hỏng: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Trạng thái Start-All dùng chung bị hỏng; kiểm tra data/bulk_jobs.json.",
        ) from exc
    if remote_job:
        metrics.inc("bulk_job_remote_cancel")
        return {
            "success": True,
            "job_id": remote_job.get("job_id"),
            "remote": True,
            "message": "Đã gửi yêu cầu hủy tới instance đang chạy Start-All.",
        }
    return {"success": False, "message": "Không có tiến trình Start-All nào đang chạy."}


@app.post("/api/profiles/stop-all")
async def stop_all_profiles(body: Optional[StopAllBody] = None):
    ids = body.profile_ids if body else None
    if ids is not None and not ids:
        raise HTTPException(
            status_code=400,
            detail="Danh sách profile được chọn trống. Bỏ profile_ids để dừng toàn bộ.",
        )
    return await asyncio.to_thread(launcher.stop_all, ids)


@app.get("/api/profiles/export")
async def export_profiles(request: Request):
    """Xuất metadata toàn bộ profile ra JSON (KHÔNG kèm secret).

    Mật khẩu proxy không bao giờ xuất (DPAPI gắn máy — import sang máy khác
    cũng không dùng được). Yêu cầu header x-confirm-export: yes như cookies.
    """
    if request.headers.get("x-confirm-export") != "yes":
        raise HTTPException(status_code=403, detail="Cần xác nhận x-confirm-export: yes trước khi export.")
    profiles = profile_manager.list_profiles()
    items = []
    for p in profiles:
        d = p.model_dump()
        d.pop("id", None)
        d.pop("status", None)
        d.pop("port", None)
        d.pop("pid", None)
        d.pop("revision", None)
        if d.get("proxy"):
            d["proxy"]["password"] = None
        items.append(d)
    from datetime import datetime

    payload = {
        "version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(items),
        "profiles": items,
    }
    filename = f"antidetect-profiles-{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _resolve_browser_paths(profiles: List[Profile]) -> Dict[str, Optional[str]]:
    """Resolve the exact browser binary per profile without launching it."""
    return {p.id: launcher._resolve_browser_path(p) for p in profiles}


@app.get("/api/profiles/audit")
async def audit_profiles_endpoint():
    """Fingerprint Health Center: static audit, no browser launched."""
    profiles = await asyncio.to_thread(profile_manager.list_profiles)
    browser_paths = await asyncio.to_thread(_resolve_browser_paths, profiles)
    return await asyncio.to_thread(audit_profiles, profiles, browser_paths)


@app.post("/api/profiles/audit/fix")
async def fix_profiles_endpoint(req: AuditFixRequest):
    """Apply safe fingerprint repairs (dry_run by default)."""
    profiles = await asyncio.to_thread(profile_manager.list_profiles)
    selected_ids: Optional[Set[str]] = None
    if req.profile_ids is not None:
        if not req.profile_ids:
            raise HTTPException(
                status_code=400,
                detail="Danh sách profile được chọn trống. Bỏ profile_ids để kiểm tra toàn bộ.",
            )
        wanted = set(req.profile_ids)
        existing = {p.id for p in profiles}
        missing = sorted(wanted - existing)
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"Không tìm thấy profile: {', '.join(missing[:5])}",
            )
        selected_ids = wanted

    browser_paths = await asyncio.to_thread(_resolve_browser_paths, profiles)
    # Cross-profile rules need the full set; the selection only scopes the
    # updates and decides whose seeds are reassigned.
    plan = await asyncio.to_thread(
        build_fix_plan, profiles, browser_paths, selected_ids
    )
    scoped = (
        [p for p in profiles if p.id in selected_ids]
        if selected_ids is not None
        else profiles
    )
    if selected_ids is not None:
        plan = [item for item in plan if item["profile_id"] in selected_ids]
    summary = {
        "profiles": len(scoped),
        "fixable_profiles": len(plan),
        "field_changes": sum(len(item["changes"]) for item in plan),
    }
    if req.dry_run:
        return {
            "dry_run": True,
            "changed": [],
            "changes": plan,
            "summary": summary,
            "message": (
                f"{len(plan)} profile có thể sửa an toàn; chưa ghi thay đổi nào."
                if plan
                else "Không có thay đổi an toàn nào cần áp dụng."
            ),
        }

    updates = {
        item["profile_id"]: FingerprintConfig.model_validate(item["fingerprint"])
        for item in plan
    }
    if not updates:
        report = await asyncio.to_thread(audit_profiles, profiles, browser_paths)
        return {
            "dry_run": False,
            "changed": [],
            "changes": [],
            "summary": summary,
            "report": report,
            "message": "Không có thay đổi an toàn nào cần áp dụng.",
        }

    expected = req.expected_revisions or {
        p.id: int(p.revision or 1) for p in profiles
    }
    try:
        changed = await asyncio.to_thread(
            profile_manager.update_fingerprints_batch, updates, expected
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    fresh_profiles = await asyncio.to_thread(profile_manager.list_profiles)
    fresh_browser_paths = await asyncio.to_thread(
        _resolve_browser_paths, fresh_profiles
    )
    report = await asyncio.to_thread(
        audit_profiles, fresh_profiles, fresh_browser_paths
    )
    return {
        "dry_run": False,
        "changed": changed,
        "changes": plan,
        "summary": summary,
        "report": report,
        "message": f"Đã sửa an toàn fingerprint cho {len(changed)} profile.",
    }


@app.get("/api/settings/launch-gate")
async def get_launch_gate_settings():
    """Static pre-launch gate configuration (no profile is launched)."""
    return _launch_gate_settings()


@app.put("/api/settings/launch-gate")
async def update_launch_gate_settings(req: LaunchGateSettingsRequest):
    try:
        await asyncio.to_thread(_set_launch_gate_mode, req.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    snapshot = _launch_gate_settings()
    snapshot["message"] = (
        f"Đã đặt cổng fingerprint: {snapshot['labels'].get(snapshot['mode'], snapshot['mode'])}."
    )
    if snapshot.get("env_override"):
        snapshot["message"] += (
            " Biến môi trường ANTIDETECT_LAUNCH_GATE đang ghi đè cấu hình này."
        )
    return snapshot


@app.get("/api/profiles/{profile_id}", response_model=Profile)
async def get_profile(profile_id: str):
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    if launcher.is_running(p.id):
        p.status = "running"
        p.port = launcher.active_ports.get(p.id)
    else:
        p.status = "stopped"
    return p


@app.put("/api/profiles/{profile_id}", response_model=Profile)
async def update_profile_endpoint(profile_id: str, req: UpdateProfileRequest):
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")

    if req.name is not None:
        p.name = req.name
    if req.notes is not None:
        p.notes = req.notes
    if req.tags is not None:
        p.tags = req.tags
    if req.proxy is not None:
        same_endpoint = bool(
            p.proxy
            and req.proxy.identity_key() == p.proxy.identity_key()
        )
        if (
            same_endpoint
            and p.proxy
            and p.proxy.password
            and req.proxy.password in (None, "", REDACTED_SECRET)
        ):
            req.proxy.password = p.proxy.password
        p.proxy = req.proxy
    if req.fingerprint is not None:
        p.fingerprint = req.fingerprint
    if req.browser_path is not None:
        # "" = reset về browser mặc định của tool.
        p.browser_path = req.browser_path.strip() or None

    try:
        updated = profile_manager.update_profile(
            p, expected_revision=req.expected_revision
        )
    except RuntimeError as e:
        # expected_revision không khớp: profile đã bị client khác sửa.
        raise HTTPException(status_code=409, detail=str(e)) from e
    if not updated:
        # Profile biến mất giữa get và update (bị xóa bởi phiên khác).
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    return updated


@app.post("/api/profiles/{profile_id}/reset-fingerprint", response_model=Profile)
async def reset_fingerprint_endpoint(profile_id: str, target_os: Optional[str] = None):
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")

    os_to_use = target_os or p.fingerprint.os
    timezone = p.fingerprint.timezone
    locale = p.fingerprint.locale
    lat = p.fingerprint.latitude
    lon = p.fingerprint.longitude

    # Nếu profile có proxy, kiểm tra xem có cần update geo/locale không
    if p.proxy and p.proxy.enabled and p.proxy.host and p.proxy.port:
        geo = await asyncio.to_thread(check_proxy, p.proxy, 4)
        if geo.get("success"):
            timezone = geo.get("timezone", timezone)
            locale = locale_for_country(geo.get("country_code"), default=locale)
            lat = geo.get("lat", lat)
            lon = geo.get("lon", lon)

    from antidetect.fingerprint import get_location_coordinates
    auto_lat, auto_lon, _ = get_location_coordinates("hanoi", randomize=True)
    new_fp = generate_fingerprint(
        target_os=os_to_use,
        timezone=timezone,
        locale=locale,
        latitude=lat if lat is not None else auto_lat,
        longitude=lon if lon is not None else auto_lon,
        browser_brand=detect_browser_brand(getattr(launcher, "browser_path", None)),
    )
    p.fingerprint = new_fp
    updated = profile_manager.update_profile(p)
    if not updated:
        # Profile bị xóa bởi phiên khác giữa get và update.
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    return updated


@app.get("/api/fingerprint/options")
async def get_fingerprint_options():
    from antidetect.fingerprint import (
        GPU_PROFILES,
        SCREEN_RESOLUTIONS,
        HARDWARE_PRESETS,
        POPULAR_LOCATIONS,
        get_host_device_memory,
        get_host_screen_info,
        get_installed_chrome_version,
        host_os,
    )
    ver_info = get_installed_chrome_version()
    browser_ver = ver_info[1] if ver_info else "155.0.8059.85"
    host_width, host_height, host_scale = get_host_screen_info()
    common_timezones = [
        "Asia/Ho_Chi_Minh",
        "America/New_York",
        "America/Los_Angeles",
        "America/Chicago",
        "Europe/London",
        "Europe/Paris",
        "Europe/Berlin",
        "Asia/Tokyo",
        "Asia/Seoul",
        "Asia/Singapore",
        "Asia/Bangkok",
        "Australia/Sydney"
    ]
    return {
        "gpu_profiles": GPU_PROFILES,
        "screen_resolutions": SCREEN_RESOLUTIONS,
        "hardware_presets": HARDWARE_PRESETS,
        "timezones": common_timezones,
        "locations": POPULAR_LOCATIONS,
        "browser_version": browser_ver,
        "host": {
            "os": host_os(),
            "screen_width": host_width,
            "screen_height": host_height,
            "device_pixel_ratio": round(max(0.5, host_scale), 2),
            "device_memory": get_host_device_memory(),
        },
    }


@app.post("/api/profiles/{profile_id}/set-location", response_model=Profile)
async def set_profile_location(profile_id: str, req: SwitchLocationRequest):
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    from antidetect.fingerprint import get_location_coordinates
    if req.latitude is not None and req.longitude is not None:
        if not (-90.0 <= req.latitude <= 90.0 and -180.0 <= req.longitude <= 180.0):
            raise HTTPException(status_code=400, detail="Tọa độ không hợp lệ: -90 <= lat <= 90, -180 <= lon <= 180")
        lat = req.latitude
        lon = req.longitude
        tz = req.timezone or p.fingerprint.timezone
    else:
        lat, lon, tz = get_location_coordinates(req.city, randomize=True)
        if req.timezone:
            tz = req.timezone

    if not validate_iana_timezone(tz):
        raise HTTPException(status_code=400, detail=f"Múi giờ IANA '{tz}' không hợp lệ")

    p.fingerprint.latitude = lat
    p.fingerprint.longitude = lon
    p.fingerprint.timezone = tz
    updated = profile_manager.update_profile(p)
    if not updated:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    return updated


def _scheduler_start_all(profile_ids=None):
    """Callback scheduler: Scheduler.run gọi cb(ids) positionally.

    Không truyền thẳng launcher.launch_all vì tham số đầu của nó là ``delay``
    (list ID sẽ bị hiểu thành delay) và sẽ raise TypeError.

    Job theo lịch dùng chung slot với Start-All thủ công: nếu slot đang bận
    (cùng process hoặc instance khác) thì bỏ lượt, để không mở trùng browser
    và không giữ claim của lịch suốt thời gian chờ.
    """
    global _active_start_all_job, _start_all_cancel_event
    if not _bulk_launch_lock.acquire(blocking=False):
        metrics.inc("scheduler_start_all_skipped")
        log.warning("Bỏ qua Start-All theo lịch: một bulk job khác đang chạy.")
        return {"success": False, "skipped": True, "reason": "bulk_busy"}
    job_id = uuid.uuid4().hex
    cancel_event = threading.Event()
    heartbeat = None
    try:
        profiles = _profiles_with_status()
        if profile_ids is not None:
            wanted = set(profile_ids)
            profiles = [p for p in profiles if p.id in wanted]
        to_start = [p for p in profiles if p.status != "running"]
        gate_mode = _launch_gate_mode()
        blocked = []
        allowed_to_start = to_start
        launch_ids = profile_ids
        if gate_mode != "off":
            browser_paths = _resolve_browser_paths(profiles)
            gate_by_id = _evaluate_launch_gates(
                profiles, browser_paths, gate_mode
            )
            blocked = [
                gate_by_id[p.id]
                for p in to_start
                if p.id in gate_by_id and not gate_by_id[p.id].allowed
            ]
            blocked_ids = {item.profile_id for item in blocked}
            allowed_to_start = [p for p in to_start if p.id not in blocked_ids]
            if to_start and not allowed_to_start:
                metrics.inc("scheduler_start_all_gate_blocked")
                log.warning(
                    "Bỏ qua Start-All theo lịch: cổng fingerprint chặn %d profile.",
                    len(blocked),
                )
                return {
                    "success": False,
                    "skipped": True,
                    "reason": "launch_gate_blocked",
                    "blocked": [item.as_dict() for item in blocked],
                }
            if blocked_ids:
                base_ids = (
                    list(profile_ids)
                    if profile_ids is not None
                    else [p.id for p in profiles]
                )
                launch_ids = [pid for pid in base_ids if pid not in blocked_ids]
        with _start_all_lock:
            if _active_start_all_job is not None and _active_start_all_job.get(
                "status"
            ) in ("running", "cancelling"):
                metrics.inc("scheduler_start_all_skipped")
                log.warning(
                    "Bỏ qua Start-All theo lịch: job %s đang chạy.",
                    _active_start_all_job.get("job_id"),
                )
                return {"success": False, "skipped": True, "reason": "bulk_busy"}
        try:
            claimed, existing = _bulk_job_store.try_claim(
                job_id=job_id,
                owner_instance=app.state.instance_id,
                kind="scheduled_start_all",
                total=len(profiles),
                starting_count=len(allowed_to_start),
            )
        except (BulkJobStoreCorruption, TimeoutError) as exc:
            raise RuntimeError(f"bulk job state: {exc}") from exc
        if not claimed:
            metrics.inc("scheduler_start_all_skipped")
            log.warning(
                "Bỏ qua Start-All theo lịch: instance %s đang chạy job %s.",
                existing.get("owner_instance"), existing.get("job_id"),
            )
            return {
                "success": False,
                "skipped": True,
                "reason": "remote_bulk_job",
                "job_id": existing.get("job_id"),
            }
        with _start_all_lock:
            _start_all_cancel_event = cancel_event
            _active_start_all_job = {
                "job_id": job_id,
                "kind": "scheduled_start_all",
                "status": "running",
                "owner_instance": app.state.instance_id,
                "total": len(profiles),
                "starting_count": len(allowed_to_start),
                "blocked_count": len(blocked),
                "gate_mode": gate_mode,
                "completed_count": 0,
                "failed_count": 0,
                "max_concurrent": 0,
                "results": [],
                "start_time": time.time(),
                "heartbeat_at": time.time(),
                "cancel_requested": False,
            }
        heartbeat = BulkJobHeartbeat(
            _bulk_job_store, job_id, app.state.instance_id, cancel_event
        )
        status = "completed"
        error = None
        try:
            heartbeat.start()
            results = launcher.launch_all(
                delay=0.8,
                cancel_event=cancel_event,
                progress_callback=lambda p_id, res: _record_bulk_progress(
                    job_id, p_id, res
                ),
                profile_ids=launch_ids,
            )
            def _launch_failed(item):
                result = item.get("result") or {}
                if not result.get("success"):
                    return True
                if result.get("cdp_ready") is False:
                    return True
                state = (result.get("cdp_health") or {}).get("state")
                return bool(state and state != "healthy")

            failures = [
                {
                    "profile_id": item.get("profile_id"),
                    "code": (item.get("result") or {}).get("code"),
                }
                for item in results
                if _launch_failed(item)
            ]
            if cancel_event.is_set():
                status = "cancelled"
            elif failures:
                status = "partial"
            return {
                "success": not failures,
                "job_id": job_id,
                "status": status,
                "failed_count": len(failures),
                "failures": failures,
                "results": results,
                "blocked": [item.as_dict() for item in blocked],
                "gate_mode": gate_mode,
            }
        except Exception as exc:
            status = "failed"
            error = str(exc)[:500]
            raise
        finally:
            heartbeat.stop()
            _finish_bulk_job(job_id, status, error)
    finally:
        _bulk_launch_lock.release()


def _scheduler_sync_all_locations(profile_ids=None):
    """Callback scheduler (sync, chạy trong thread scheduler)."""
    profiles = profile_manager.list_profiles()
    if profile_ids:
        wanted = set(profile_ids)
        profiles = [p for p in profiles if p.id in wanted]
    done, skipped = 0, 0
    for p in profiles:
        if not (p.proxy and p.proxy.enabled and p.proxy.host):
            skipped += 1
            continue
        try:
            geo = check_proxy(p.proxy, 12)
            if geo.get("success"):
                if _apply_geo_to_profile(p, geo, profile_manager.update_profile):
                    done += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1
    return {"synced": done, "skipped": skipped, "total": len(profiles)}


def _scheduler_sync_fanpages(profile_ids=None):
    """Callback scheduler: đưa job đồng bộ danh sách fanpage (discovery) vào hàng đợi."""
    svc = get_fanpage_service()
    job, deduplicated = svc.enqueue_sync_job(
        mode="discovery",
        profile_ids=profile_ids,
        auto_start=True,
        triggered_by="scheduler",
        run_async=True,
    )
    return {"job_id": job.get("id"), "deduplicated": deduplicated}


def _scheduler_sync_fanpages_deep(profile_ids=None):
    """Callback scheduler: đưa job đồng bộ chi tiết fanpage (basic) vào hàng đợi."""
    svc = get_fanpage_service()
    job, deduplicated = svc.enqueue_sync_job(
        mode="basic",
        profile_ids=profile_ids,
        auto_start=True,
        triggered_by="scheduler",
        run_async=True,
    )
    return {"job_id": job.get("id"), "deduplicated": deduplicated}


@app.post("/api/profiles/{profile_id}/sync-location-proxy")
async def sync_location_from_proxy(profile_id: str):
    """Giai đoạn 1 anti-detection: đồng bộ timezone/geo/locale theo IP proxy.

    Đổi proxy sau khi tạo profile là lỗi nhất quán phổ biến nhất (IP Mỹ +
    timezone Hà Nội → detector flag ngay). Endpoint này hỏi GeoIP QUA proxy
    của profile và cập nhật fingerprint cho khớp; trả về từng thay đổi để
    UI/automation hiển thị.
    """
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    if not (p.proxy and p.proxy.enabled and p.proxy.host):
        raise HTTPException(status_code=400, detail="Profile chưa bật proxy — không có IP để đồng bộ.")

    geo = await asyncio.to_thread(check_proxy, p.proxy, 12)
    if not geo.get("success"):
        raise HTTPException(status_code=502, detail=f"Không truy vấn được GeoIP qua proxy: {geo.get('error') or 'lỗi không xác định'}")

    changes = await asyncio.to_thread(
        _apply_geo_to_profile, p, geo, profile_manager.update_profile
    )
    if not profile_manager.get_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    return {
        "success": True,
        "geo": {"ip": geo.get("ip"), "country": geo.get("country"),
                "city": geo.get("city"), "isp": geo.get("isp")},
        "changes": changes,
        "message": "Fingerprint đã khớp IP proxy." if changes
                   else "Fingerprint đã khớp sẵn với IP proxy — không cần thay đổi.",
    }


@app.post("/api/profiles/switch-location-all")
async def switch_all_profiles_location(req: SwitchLocationRequest):
    """P1-03: Chuyển đổi vị trí toàn bộ profiles, kiểm tra 0 profiles, validate ZoneInfo."""
    from antidetect.fingerprint import get_location_coordinates

    custom_coords = req.latitude is not None and req.longitude is not None
    if custom_coords:
        if not (-90.0 <= req.latitude <= 90.0 and -180.0 <= req.longitude <= 180.0):
            raise HTTPException(status_code=400, detail="Tọa độ không hợp lệ: -90 <= lat <= 90, -180 <= lon <= 180")

    if req.timezone and not validate_iana_timezone(req.timezone):
        raise HTTPException(status_code=400, detail=f"Múi giờ IANA '{req.timezone}' không hợp lệ")

    def run_switch():
        # Chạy trong thread: list + update từng profile + is_running đều là
        # I/O/PowerShell đồng bộ. Batch ghi profiles.json đúng MỘT lần thay vì
        # N lần read+backup+write như từng update_profile riêng lẻ.
        profiles = profile_manager.list_profiles()
        if not profiles:
            return None, []
        updates = []
        for p in profiles:
            if custom_coords:
                lat = req.latitude
                lon = req.longitude
                tz = req.timezone or p.fingerprint.timezone
            else:
                lat, lon, tz = get_location_coordinates(req.city, randomize=True)
                if req.timezone:
                    tz = req.timezone
            if not validate_iana_timezone(tz):
                raise ValueError(f"Múi giờ IANA '{tz}' không hợp lệ")
            p.fingerprint.latitude = lat
            p.fingerprint.longitude = lon
            p.fingerprint.timezone = tz
            updates.append((p, lat, lon, tz))

        updated_ids = set(
            profile_manager.update_profiles_batch([p for p, _, _, _ in updates])
        )
        results = []
        for p, lat, lon, tz in updates:
            results.append({
                "id": p.id,
                "name": p.name,
                "city": req.city,
                "latitude": lat,
                "longitude": lon,
                "timezone": tz,
                "running": launcher.is_running(p.id),
                "updated": p.id in updated_ids,
            })
        return results, [p.id for p, _, _, _ in updates if p.id not in updated_ids]

    try:
        results, failed_ids = await asyncio.to_thread(run_switch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if results is None:
        raise HTTPException(status_code=404, detail="Không có profile nào để chuyển đổi vị trí")

    ok_count = len([r for r in results if r["updated"]])
    message = (
        f"Đã chuyển vị trí {ok_count} profile sang {req.city.upper()} thành công!"
        if not failed_ids
        else f"Đã chuyển {ok_count}/{len(results)} profile; {len(failed_ids)} profile không ghi được (đã bị thay đổi bởi phiên khác)."
    )
    return {
        "success": not failed_ids,
        "message": message,
        "count": ok_count,
        "failed_profile_ids": failed_ids,
        "profiles": results
    }


@app.get("/api/system/logs")
async def get_system_logs(lines: int = 200):
    """Đuôi app.log để xem nhật ký hệ thống ngay trên dashboard (read-only)."""
    lines = max(10, min(1000, int(lines or 200)))

    def read_tail():
        log_file = os.path.join(DATA_DIR, "app.log")
        if not os.path.exists(log_file):
            return []
        size = os.path.getsize(log_file)
        with open(log_file, "rb") as f:
            f.seek(max(0, size - 256 * 1024))  # đọc tối đa 256KB cuối
            data = f.read().decode("utf-8", "replace")
        return data.splitlines()[-lines:]

    try:
        tail = await asyncio.to_thread(read_tail)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Không đọc được app.log: {e}")
    return {"success": True, "lines": tail, "count": len(tail)}


_token_rotate_lock = threading.Lock()
_token_rotate_times: list = []


@app.post("/api/auth/token/rotate")
async def rotate_api_token(request: Request):
    """Tạo API token dài hạn mới (token cũ vô hiệu ngay). Trả plaintext 1 lần."""
    if not authorized(request):
        raise HTTPException(status_code=401, detail='Chưa xác thực')
    now = time.time()
    with _token_rotate_lock:
        _token_rotate_times[:] = [t for t in _token_rotate_times if now - t < 60]
        if len(_token_rotate_times) >= 5:
            raise HTTPException(status_code=429, detail='Quá nhiều lần tạo token trong 1 phút.')
        _token_rotate_times.append(now)
    import hashlib as _hashlib
    token = secrets.token_urlsafe(32)
    from antidetect.runtime_auth import write_api_token_hash
    await asyncio.to_thread(
        write_api_token_hash,
        _hashlib.sha256(token.encode('utf-8')).hexdigest(),
    )
    return {"success": True, "token": token,
            "note": "Token chỉ hiển thị MỘT lần này. Lưu ngay vào chỗ an toàn; làm mất chỉ có thể rotate token mới."}


@app.delete("/api/auth/token")
async def revoke_api_token(request: Request):
    """Thu hồi API token dài hạn."""
    if not authorized(request):
        raise HTTPException(status_code=401, detail='Chưa xác thực')
    from antidetect.runtime_auth import delete_api_token_hash
    await asyncio.to_thread(delete_api_token_hash)
    return {"success": True, "message": "Đã thu hồi API token."}


# --- Đợt C: Lịch tự động ---

SCHEDULES_CORRUPT_DETAIL = (
    "Dữ liệu lịch bị hỏng; hãy khôi phục file schedules.json hợp lệ."
)


@app.get("/api/schedules")
async def list_schedules():
    if _schedule_store is None:
        raise HTTPException(status_code=503, detail="Scheduler chưa khởi động")
    try:
        rules = _schedule_store.list()
    except ScheduleStoreCorruption as e:
        raise HTTPException(status_code=503, detail=SCHEDULES_CORRUPT_DETAIL) from e
    recent_runs = _scheduler.run_history.recent(20) if _scheduler else []
    return {"success": True, "rules": rules,
            "last_tick": (_scheduler.last_run_info.get("last_tick") if _scheduler else None),
            "recent_runs": recent_runs}


@app.post("/api/schedules")
async def create_schedule(req: ScheduleCreateRequest):
    if _schedule_store is None:
        raise HTTPException(status_code=503, detail="Scheduler chưa khởi động")
    try:
        rule = _schedule_store.add(req.name, req.time, req.action,
                                   req.profile_ids, req.enabled)
    except ScheduleStoreCorruption as e:
        raise HTTPException(status_code=503, detail=SCHEDULES_CORRUPT_DETAIL) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, "rule": rule}


@app.put("/api/schedules/{rule_id}")
async def update_schedule(rule_id: str, req: ScheduleUpdateRequest):
    if _schedule_store is None:
        raise HTTPException(status_code=503, detail="Scheduler chưa khởi động")
    try:
        rule = _schedule_store.update(rule_id, **req.model_dump(exclude_unset=True))
    except ScheduleStoreCorruption as e:
        raise HTTPException(status_code=503, detail=SCHEDULES_CORRUPT_DETAIL) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if rule is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy lịch")
    return {"success": True, "rule": rule}


@app.delete("/api/schedules/{rule_id}")
async def delete_schedule(rule_id: str):
    if _schedule_store is None:
        raise HTTPException(status_code=503, detail="Scheduler chưa khởi động")
    try:
        deleted = _schedule_store.delete(rule_id)
    except ScheduleStoreCorruption as e:
        raise HTTPException(status_code=503, detail=SCHEDULES_CORRUPT_DETAIL) from e
    if not deleted:
        raise HTTPException(status_code=404, detail="Không tìm thấy lịch")
    return {"success": True}


@app.get("/api/system/current-ip")
async def get_current_network_ip():
    """Return the host's public IP/ISP using HTTPS-only GeoIP services."""
    from antidetect.proxy import get_public_ip_info

    try:
        data = await asyncio.to_thread(get_public_ip_info, None, 6)
        if data.get("success"):
            return data
        return {"success": False, "error": data.get("error", "GeoIP lookup failed")}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/system/shutdown")
async def shutdown_system():
    server = getattr(app.state, 'server', None)
    if server is None:
        raise HTTPException(status_code=409, detail='Hãy dừng máy chủ qua terminal đã khởi chạy nó.')
    result = await asyncio.to_thread(launcher.stop_all)
    if not result['success']:
        raise HTTPException(status_code=409, detail='Còn profile chưa đóng. Máy chủ được giữ lại để bạn thử lại.')
    asyncio.get_running_loop().call_later(0.5, setattr, server, 'should_exit', True)
    return {"success": True, "message": "Hệ thống đang tắt. Bạn có thể đóng tab này an toàn."}


@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: str):
    stopped = await asyncio.to_thread(launcher.stop, profile_id)
    if not stopped.get('success'):
        error = stopped.get('error', 'Chưa đóng được profile')
        # "Profile không tồn tại" là lỗi không tìm thấy (404), không phải xung đột.
        if 'không tồn tại' in error.lower():
            raise HTTPException(status_code=404, detail=error)
        raise HTTPException(status_code=409, detail=error)
    try:
        success = await asyncio.to_thread(profile_manager.delete_profile, profile_id)
    except ProfileDeletionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        # ID không hợp lệ (chống path traversal) phải là 400, không phải 500.
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not success:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    return {"success": True, "message": "Đã xóa profile thành công"}


@app.post("/api/profiles/{profile_id}/duplicate", response_model=Profile)
async def duplicate_profile(
    profile_id: str,
    request: Request,
    response: Response,
    copy_data: bool = False,
):
    header_key = request.headers.get("idempotency-key")
    operation_id = header_key or new_operation_id()
    if header_key:
        try:
            validate_idempotency_key(header_key)
        except ProfileCopyError as e:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "code": e.code,
                    "detail": str(e),
                },
            )
    response.headers["X-Operation-Id"] = operation_id

    def _duplicate():
        return profile_manager.duplicate_profile(
            profile_id,
            copy_data=copy_data,
            operation_id=operation_id,
            source_running=launcher.profile_data_in_use,
        )

    try:
        result = await asyncio.to_thread(_duplicate)
    except ProfileCopyError as e:
        if e.code in ("SOURCE_RUNNING", "PROFILE_BUSY", "COPY_OPERATION_CONFLICT"):
            status_code = 409
        elif e.code == "COPY_NO_SPACE":
            status_code = 507
        elif e.code == "INVALID_IDEMPOTENCY_KEY":
            status_code = 400
        else:
            status_code = 500
        return JSONResponse(
            status_code=status_code,
            content={
                "success": False,
                "code": e.code,
                "detail": str(e),
            },
            headers={"X-Operation-Id": operation_id},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "code": "COPY_FAILED",
                "detail": str(e),
            },
            headers={"X-Operation-Id": operation_id},
        )
    if not result:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "code": "NOT_FOUND",
                "detail": "Profile không tồn tại",
            },
            headers={"X-Operation-Id": operation_id},
        )
    return result


@app.get("/api/profiles/{profile_id}/cookies/export")
async def export_profile_cookies(profile_id: str, request: Request):
    """Đọc cookies của profile đang chạy mà không ghi bản sao vào đĩa/cache."""
    if request.headers.get('x-confirm-cookie-access') != 'yes':
        raise HTTPException(status_code=403, detail='Cần xác nhận trước khi đọc cookie phiên đăng nhập.')
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    if not launcher.is_running(profile_id):
        raise HTTPException(status_code=409, detail="Hãy mở profile trước khi đọc cookies.")
    try:
        cookies = await asyncio.to_thread(launcher.export_cookies, profile_id)
    except CdpUnavailable as e:
        return _cdp_conflict_response(e)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return JSONResponse(
        content={"success": True, "profile_id": profile_id, "cookies": cookies, "count": len(cookies)},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.post("/api/profiles/{profile_id}/cookies/import")
async def import_profile_cookies(profile_id: str, req: ImportCookiesRequest):
    """Nạp cookies vào profile đang chạy, không lưu bản sao JSON trên đĩa."""
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    if not isinstance(req.cookies, list):
        raise HTTPException(status_code=400, detail="Định dạng cookies không hợp lệ, yêu cầu một mảng JSON các cookie.")
    if not launcher.is_running(profile_id):
        raise HTTPException(status_code=409, detail="Hãy mở profile trước khi nạp cookies.")

    def _import():
        # Đổi danh tính Facebook không được chạy chồng với lượt quét fanpage.
        with profile_manager.scan_lock(profile_id, timeout=0.0):
            return launcher.import_cookies(profile_id, req.cookies)

    try:
        success = await asyncio.to_thread(_import)
    except ProfileLockTimeout:
        raise HTTPException(
            status_code=409,
            detail="Đang có lượt quét fanpage cho profile này. Hãy chờ lượt đó xong.",
        )
    except CdpUnavailable as e:
        return _cdp_conflict_response(e)
    if not success:
        raise HTTPException(status_code=503, detail="Không thể nạp cookies vào profile đang chạy.")
    return {
        "success": True,
        "profile_id": profile_id,
        "count": len(req.cookies),
        "message": f"Đã nạp {len(req.cookies)} cookies thành công!"
    }


def _scan_fields(scan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    scan = scan or {}
    return {
        "scan_id": scan.get("scan_id"),
        "discovery_status": scan.get("discovery_status"),
        "completion_reason": scan.get("completion_reason"),
        "stability": scan.get("stability"),
        "observed_count": scan.get("observed_count"),
        "retained_count": scan.get("retained_count"),
        "total_pages_in_source": scan.get("total_pages_in_source"),
        "started_at": scan.get("started_at"),
        "finished_at": scan.get("finished_at"),
        "identity_status": scan.get("identity_status"),
        "collector_version": scan.get("collector_version"),
    }


def _fanpage_stale(
    scan: Optional[Dict[str, Any]],
    pages: List[Dict[str, Any]],
    synced_at: int,
    now: datetime.datetime,
    stale_after_hours: int,
) -> bool:
    if not pages:
        return False
    status = (scan or {}).get("discovery_status")
    if status in (STATUS_PARTIAL, STATUS_FAILED, STATUS_UNKNOWN, None):
        return True
    if synced_at and (now.timestamp() - synced_at) > stale_after_hours * 3600:
        return True
    return any(
        pg.get("activity_stale") or pg.get("activity_scan_complete") is False
        for pg in pages
    )


def _run_facebook_scan(
    profile_id: str, deep: bool, force: bool, stale_after_hours: int
):
    """Chạy trong thread: giữ scan lock suốt fetch + merge (QĐ-6).

    Scan lock chỉ chặn lượt quét khác, không chặn stop/delete; profile dừng
    giữa chừng làm collector ném ``ScanAborted`` và cache giữ nguyên.
    """
    with profile_manager.scan_lock(profile_id, timeout=0.0):
        profile = profile_manager.get_profile(profile_id)
        if profile is None:
            return None
        scan_meta = profile_manager.get_facebook_scan_meta(profile_id)
        data = launcher.fetch_facebook_pages(
            profile_id,
            DISCOVERY_ENDPOINT_TIMEOUT_S,
            deep,
        )
        scan = ensure_scan_contract(
            data, expected_account_id=scan_meta.get("account_id_observed")
        )
        applied = profile_manager.apply_facebook_scan(profile_id, scan)
        if applied is None:
            return None
        updated, scan = applied
        if (
            force
            and scan.get("discovery_status") in (STATUS_COMPLETE, STATUS_PARTIAL)
            and scan.get("observed_count") == 0
        ):
            cleared = profile_manager.clear_facebook_pages(profile_id)
            if cleared is not None:
                updated = cleared
            scan["retained_count"] = 0

        if scan.get("completion_reason") == "LOGIN_LOST":
            return {
                "success": False,
                "logged_in": False,
                "login_status": False,
                "pages": [],
                "source": "live",
                "stale": False,
                **_scan_fields(scan),
                "detail": (
                    "Profile chưa đăng nhập Facebook. "
                    "Hãy mở profile, đăng nhập rồi thử lại."
                ),
            }

        now = datetime.datetime.now(datetime.timezone.utc)
        enriched = _enrich_facebook_pages(
            updated.facebook_pages or [],
            now=now,
            timezone=_profile_display_timezone(updated),
            stale_after_hours=stale_after_hours,
        )
        observed = int(scan.get("observed_count") or 0)
        return {
            "success": True,
            "logged_in": bool(scan.get("login_status")),
            "login_status": bool(scan.get("login_status")),
            "pages": enriched,
            "source": "live" if observed else "cache",
            "stale": _fanpage_stale(
                scan, enriched, updated.facebook_pages_synced_at,
                now, stale_after_hours,
            ),
            "synced_at": updated.facebook_pages_synced_at,
            **_scan_fields(scan),
            "detail": scan_detail(scan, len(enriched)),
        }


@app.get("/api/profiles/{profile_id}/facebook/pages")
async def get_facebook_pages(
    profile_id: str, refresh: bool = False, deep: bool = False, force: bool = False
):
    """Liệt kê fanpage Facebook profile này sở hữu/quản lý.

    ``refresh=1`` (hoặc khi profile đang chạy mà chưa có cache) đọc trực tiếp
    từ phiên Chrome đang chạy. Discovery trả hợp đồng F4: ``discovery_status``
    (complete/partial/failed), ``completion_reason``, ``observed_count``,
    ``retained_count`` — tổng cache không bị báo nhầm là tổng vừa đọc.

    ``deep=1`` (kèm refresh) quét thêm TỪNG page: ngày tạo (trang about),
    khoảng thời gian bài gần nhất (feed) → trạng thái active/long_absent/
    uncertain/unknown và "sẵn sàng" (tuổi ≥ 30 ngày). Mỗi page tốn ~4-8s khi
    deep — với 20 page mất ~2-3 phút.

    Response cache trả ``login_status="unknown"`` (không khẳng định phiên còn
    đăng nhập); ``logged_in`` chỉ true/false khi vừa kiểm tra live. ``stale``
    phản ánh tuổi dữ liệu/độ đầy đủ của lần quét tương ứng, không luôn false.
    Cache không bao giờ bị xóa bởi refresh thông thường; dùng DELETE để xóa.
    """
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    running = await asyncio.to_thread(launcher.is_running, profile_id)
    now = datetime.datetime.now(datetime.timezone.utc)
    display_tz = _profile_display_timezone(p)
    stale_after_hours = _fanpage_stale_after_hours()

    if refresh or (running and not p.facebook_pages):
        if not running:
            raise HTTPException(
                status_code=409,
                detail="Hãy mở profile trước khi làm mới danh sách fanpage.",
            )
        try:
            result = await asyncio.to_thread(
                _run_facebook_scan,
                profile_id,
                bool(deep),
                bool(force),
                stale_after_hours,
            )
        except ProfileLockTimeout as e:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": (
                        "Đang có lượt quét fanpage khác cho profile này. "
                        "Hãy chờ lượt đó xong rồi thử lại."
                    )
                },
            )
        except ScanAborted as e:
            return JSONResponse(
                status_code=409,
                content={"detail": str(e), "code": "SCAN_ABORTED"},
            )
        except CdpUnavailable as e:
            return _cdp_conflict_response(e)
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        if result is None:
            raise HTTPException(status_code=404, detail="Profile không tồn tại")
        return result

    # Cache: chưa kiểm tra phiên đăng nhập nên không khẳng định logged_in.
    enriched = _enrich_facebook_pages(
        p.facebook_pages,
        now=now,
        timezone=display_tz,
        stale_after_hours=stale_after_hours,
    )
    meta = profile_manager.get_facebook_scan_meta(profile_id)
    last_scan = meta.get("last_scan") if isinstance(meta.get("last_scan"), dict) else {}
    scan: Dict[str, Any] = dict(scan_defaults(len(enriched)))
    scan.update({k: v for k, v in last_scan.items() if v is not None})
    # Cache không xác nhận phiên đăng nhập hiện tại.
    scan["login_status"] = "unknown"
    scan["identity_status"] = (
        meta.get("identity_status") or scan.get("identity_status") or "unknown"
    )
    stale = _fanpage_stale(
        scan, enriched, p.facebook_pages_synced_at, now, stale_after_hours
    )
    return {
        "success": True,
        "logged_in": None,
        "login_status": "unknown",
        "pages": enriched,
        "source": "cache",
        "stale": stale,
        "synced_at": p.facebook_pages_synced_at,
        **_scan_fields(scan),
        "detail": scan_detail(scan, len(enriched)),
    }


@app.delete("/api/profiles/{profile_id}/facebook/pages")
async def delete_facebook_pages(profile_id: str):
    """Xóa cache fanpage theo hành động rõ ràng (không tự xóa khi refresh)."""
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")

    def _clear():
        with profile_manager.scan_lock(profile_id, timeout=0.0):
            return profile_manager.clear_facebook_pages(profile_id)

    try:
        updated = await asyncio.to_thread(_clear)
    except ProfileLockTimeout:
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    "Đang có lượt quét fanpage cho profile này. "
                    "Hãy chờ lượt đó xong rồi xóa cache."
                )
            },
        )
    if updated is None:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    return {
        "success": True,
        "pages": [],
        "synced_at": updated.facebook_pages_synced_at,
        "detail": "Đã xóa cache fanpage của profile.",
    }


@app.post("/api/profiles/import")
async def import_profiles(req: ImportProfilesRequest):
    """Import profiles từ export JSON. Id mới tự sinh; password proxy phải nhập lại."""
    entries = req.profiles or []
    if not entries:
        raise HTTPException(status_code=400, detail="Không có profile nào trong payload (profiles: []).")
    if len(entries) > 200:
        raise HTTPException(status_code=400, detail="Tối đa 200 profile mỗi lần import.")

    imported, failed = [], []
    for i, entry in enumerate(entries):
        name = str(entry.get("name") or f"Imported {i + 1}") if isinstance(entry, dict) else f"#{i + 1}"
        try:
            if not isinstance(entry, dict):
                raise ValueError("entry không phải object")
            entry = dict(entry)
            for k in ("id", "status", "port", "pid", "revision"):
                entry.pop(k, None)
            proxy_data = entry.get("proxy") or {}
            if isinstance(proxy_data, dict):
                proxy_data = dict(proxy_data)
                proxy_data["password"] = None  # không bao giờ import password
            fp = FingerprintConfig.model_validate(entry.get("fingerprint") or {})
            proxy = ProxyConfig.model_validate(proxy_data)
            new_p = profile_manager.create_profile(
                name=name,
                target_os=fp.os,
                proxy=proxy,
                fingerprint=fp,
                notes=str(entry.get("notes") or ""),
                tags=list(entry.get("tags") or []),
            )
            new_p.browser_path = entry.get("browser_path") or None
            new_p.facebook_pages = _sanitize_facebook_pages(entry.get("facebook_pages"))
            new_p.facebook_pages_synced_at = int(entry.get("facebook_pages_synced_at") or 0)
            profile_manager.update_profile(new_p)
            imported.append({"id": new_p.id, "name": new_p.name})
        except Exception as exc:  # noqa: BLE001 - per-item import
            failed.append({"index": i, "name": name, "reason": f"{type(exc).__name__}: {exc}"})
    return {
        "success": len(failed) == 0,
        "imported_count": len(imported),
        "failed_count": len(failed),
        "imported": imported,
        "failed": failed,
    }


@app.post("/api/profiles/bulk-create")
async def bulk_create_profiles(req: BulkCreateRequest):
    """Tạo hàng loạt profile cùng cấu hình, fingerprint RIÊNG từng profile.

    Proxy: mỗi dòng 'protocol://user:pass@host:port' gán cho 1 profile theo
    thứ tự; thiếu proxy cho profile nào thì profile đó direct + warning
    (KHÔNG tuần hoàn cùng proxy cho nhiều profile — chống liên kết).
    """
    from antidetect.fingerprint import get_location_coordinates, host_os
    from antidetect.proxy import parse_proxy_line

    req.os = (req.os or "windows").lower()
    if req.os not in ("windows", "macos", "linux"):
        req.os = "windows"
    req.stealth_mode = (req.stealth_mode or "unique").lower()
    if req.stealth_mode not in ("unique", "authentic"):
        req.stealth_mode = "unique"
    if req.stealth_mode == "authentic" and req.os != host_os():
        raise HTTPException(
            status_code=400,
            detail=(
                "Stealth authentic chỉ hỗ trợ profile cùng hệ điều hành với "
                f"máy host ({host_os()}). Dùng unique cho profile {req.os}."
            ),
        )
    prefix = (req.name_prefix or "Profile").strip() or "Profile"

    created, warnings = [], []
    for i in range(req.count):
        name = f"{prefix} {i + 1:02d}"
        try:
            lat, lon, tz = get_location_coordinates(req.city, randomize=True)
            fp = generate_fingerprint(
                target_os=req.os, timezone=tz, locale="vi-VN",
                latitude=lat, longitude=lon, city=req.city,
                browser_brand=detect_browser_brand(getattr(launcher, "browser_path", None)),
            )
            if req.cpu:
                fp.hardware_concurrency = max(1, min(128, int(req.cpu)))
            if req.stealth_mode == "authentic":
                fp.stealth_disable = ["canvas", "webgl", "audio", "media", "worker"]
                if req.os == host_os():
                    vendor, renderer = _host_gpu_strings()
                    fp.webgl_vendor, fp.webgl_renderer = vendor, renderer
            proxy = None
            if i < len(req.proxies):
                try:
                    proxy = parse_proxy_line(req.proxies[i])
                except ValueError as exc:
                    warnings.append(f"{name}: proxy dòng {i + 1} lỗi ({exc}) → tạo direct")
            else:
                warnings.append(f"{name}: không có proxy (direct)")
            p = profile_manager.create_profile(
                name=name, target_os=req.os, proxy=proxy, fingerprint=fp,
            )
            created.append({"id": p.id, "name": p.name})
        except Exception as exc:  # noqa: BLE001 - per-item create
            warnings.append(f"{name}: tạo lỗi {type(exc).__name__}: {exc}")
    return {
        "success": len(created) > 0,
        "created_count": len(created),
        "created": created,
        "warnings": warnings,
    }


@app.post("/api/profiles/{profile_id}/start")
async def start_profile(profile_id: str, url: Optional[str] = None, force: bool = False):
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    # M13: Validate URL scheme — chỉ cho phép http:// và https://
    if url:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="URL không hợp lệ: chỉ chấp nhận http:// hoặc https://")

    gate = None
    gate_mode = _launch_gate_mode()
    if gate_mode != "off":
        try:
            already_running = await asyncio.to_thread(launcher.is_running, profile_id)
        except Exception:
            already_running = False
        # Opening a tab in a browser that is already running cannot change the
        # fingerprint presented at spawn, so the gate only applies to a launch.
        if not already_running:
            profiles = await asyncio.to_thread(profile_manager.list_profiles)
            browser_paths = await asyncio.to_thread(_resolve_browser_paths, profiles)
            gate = await asyncio.to_thread(
                _evaluate_launch_gate, p, profiles, browser_paths, gate_mode, force
            )
            if not gate.allowed:
                return JSONResponse(
                    status_code=409,
                    content={
                        "success": False,
                        "detail": gate.message,
                        "gate": gate.as_dict(),
                    },
                )

    res = await asyncio.to_thread(launcher.launch, profile_id, open_url=url)
    if not res.get("success"):
        code = res.get("code")
        if code in _CDP_CONFLICT_CODES or res.get("recovery_required"):
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "code": code,
                    "detail": res.get("error", "Lỗi khởi chạy"),
                    "cdp_health": res.get("cdp_health"),
                },
            )
        raise HTTPException(status_code=500, detail=res.get("error", "Lỗi khởi chạy"))
    if gate and gate.status in ("warning", "overridden", "unverified"):
        res["gate"] = gate.as_dict()
    return res


@app.post("/api/profiles/{profile_id}/stop")
async def stop_profile(profile_id: str):
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    res = await asyncio.to_thread(launcher.stop, profile_id)
    if not res.get("success"):
        raise HTTPException(status_code=409, detail=res.get("error", "Không thể dừng profile"))
    return res


@app.post("/api/proxy/check")
async def check_proxy_endpoint(req: CheckProxyRequest):
    if req.profile_id and not req.password:
        saved = profile_manager.get_profile(req.profile_id)
        if saved and saved.proxy and saved.proxy.password:
            same_endpoint = bool(
                saved.proxy.identity_key()
                == ProxyConfig(
                    enabled=True,
                    protocol=req.protocol,
                    host=req.host,
                    port=req.port,
                    username=req.username,
                ).identity_key()
            )
            if same_endpoint:
                req.password = saved.proxy.password
    cfg = ProxyConfig(
        enabled=True,
        protocol=req.protocol,
        host=req.host,
        port=req.port,
        username=req.username,
        password=req.password
    )
    return await asyncio.to_thread(check_proxy, cfg)


@app.get("/api/fingerprint/random")
async def random_fingerprint_endpoint(target_os: str = "windows"):
    return generate_fingerprint(target_os=target_os)


# --- Fanpage Manager (Giai đoạn 1) ---

_fanpage_service_cache: Dict[str, FanpageService] = {}
_fanpage_service_lock = threading.Lock()


def _check_profile_launch_gate_for_job(profile: Profile) -> tuple[bool, Optional[str]]:
    gate_mode = _launch_gate_mode()
    if gate_mode == "off":
        return True, None
    profiles = profile_manager.list_profiles()
    browser_paths = _resolve_browser_paths(profiles)
    gate = _evaluate_launch_gate(
        profile, profiles, browser_paths, gate_mode, False
    )
    return bool(gate.allowed), gate.message


def get_fanpage_service() -> FanpageService:
    """Khởi tạo lười (lazy) FanpageService gắn với thư mục data của profile_manager hiện hành.

    Tuyệt đối không mở/ghi SQLite ở thời điểm import module để các bài test
    hoán đổi ``profile_manager`` hoặc ``ANTIDETECT_DATA_DIR`` luôn cô lập
    100% khỏi ``data/`` thật.
    """
    import antidetect.storage as _storage_mod

    data_dir = os.path.abspath(
        getattr(profile_manager, "data_dir", None)
        or getattr(_storage_mod, "DATA_DIR", None)
        or os.environ.get("ANTIDETECT_DATA_DIR")
        or DATA_DIR
    )
    with _fanpage_service_lock:
        svc = _fanpage_service_cache.get(data_dir)
        if (
            svc is None
            or svc.profile_manager is not profile_manager
            or svc.launcher is not launcher
        ):
            svc = FanpageService(
                profile_manager=profile_manager,
                launcher=launcher,
                db_path=os.path.join(data_dir, "fanpages.sqlite"),
                launch_gate_check=_check_profile_launch_gate_for_job,
                owner_instance=getattr(app.state, "instance_id", None),
            )
            _fanpage_service_cache[data_dir] = svc
        return svc


@app.get("/fanpages", response_class=HTMLResponse)
async def fanpages_page(request: Request):
    if not authorized(request):
        return templates.TemplateResponse(
            request=request, name="unlock.html", context={}
        )
    return templates.TemplateResponse(
        request=request, name="fanpages.html", context={}
    )


@app.get("/api/fanpages/summary")
async def get_fanpages_summary(
    include_archived: bool = False,
    profile_id: Optional[str] = None,
    tag: Optional[str] = None,
):
    svc = get_fanpage_service()
    summary = await asyncio.to_thread(
        svc.get_summary,
        include_archived=include_archived,
        profile_id=profile_id,
        tag=tag,
    )
    return {"success": True, **summary}


@app.get("/api/fanpages/export")
async def export_fanpages_csv(
    q: Optional[str] = None,
    profile_id: Optional[str] = None,
    tag: Optional[str] = None,
    activity_status: Optional[str] = None,
    connection_status: Optional[str] = None,
    data_quality: Optional[str] = None,
    age_ready: Optional[str] = None,
    archived: str = "active",
    sort_by: str = "page_name",
    sort_dir: str = "asc",
):
    svc = get_fanpage_service()
    csv_data = await asyncio.to_thread(
        svc.export_csv,
        q=q or "",
        profile_id=profile_id or "",
        tag=tag or "",
        activity_status=activity_status or "",
        connection_status=connection_status or "",
        data_quality=data_quality or "",
        age_ready=age_ready,
        archived=archived,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    csv_bytes = (
        csv_data.encode("utf-8") if isinstance(csv_data, str) else csv_data
    )
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="fanpages_export.csv"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/fanpages/bulk")
async def bulk_update_fanpages(req: FanpageBulkUpdateRequest):
    if not req.page_ids:
        raise HTTPException(
            status_code=400, detail="Danh sách fanpage được chọn trống."
        )
    svc = get_fanpage_service()
    result = await asyncio.to_thread(
        svc.bulk_update,
        page_ids=req.page_ids,
        add_tags=req.add_tags,
        remove_tags=req.remove_tags,
        is_archived=req.is_archived,
    )
    return {"success": True, **result}


@app.post("/api/fanpages/backup")
async def create_fanpages_backup_endpoint(max_backups: int = 10):
    svc = get_fanpage_service()
    info = await asyncio.to_thread(svc.create_backup, max_backups=max_backups)
    return {"success": True, "backup": info}


@app.get("/api/fanpages/backups")
async def list_fanpages_backups_endpoint():
    svc = get_fanpage_service()
    backups = await asyncio.to_thread(svc.list_backups)
    return {"success": True, "backups": backups, "items": backups}


@app.post("/api/fanpages/prune")
async def prune_fanpages_data_endpoint(req: Optional[FanpagePruneRequest] = None):
    req = req or FanpagePruneRequest()
    svc = get_fanpage_service()
    stats = await asyncio.to_thread(
        svc.prune_old_data,
        metric_retention_days=req.metric_retention_days,
        job_retention_days=req.job_retention_days,
    )
    return {"success": True, **stats}


@app.get("/api/fanpages")
async def list_fanpages_endpoint(
    q: Optional[str] = None,
    profile_id: Optional[str] = None,
    tag: Optional[str] = None,
    activity_status: Optional[str] = None,
    connection_status: Optional[str] = None,
    data_quality: Optional[str] = None,
    age_ready: Optional[str] = None,
    archived: str = "active",
    sort_by: str = "page_name",
    sort_dir: str = "asc",
    page: int = 1,
    page_size: int = 50,
):
    svc = get_fanpage_service()
    result = await asyncio.to_thread(
        svc.list_pages,
        q=q or "",
        profile_id=profile_id or "",
        tag=tag or "",
        activity_status=activity_status or "",
        connection_status=connection_status or "",
        data_quality=data_quality or "",
        age_ready=age_ready,
        archived=archived,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        page_size=page_size,
    )
    return {"success": True, **result}


@app.get("/api/fanpages/{page_id}")
async def get_fanpage_detail_endpoint(page_id: str):
    svc = get_fanpage_service()
    detail = await asyncio.to_thread(svc.get_page, page_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Fanpage không tồn tại")
    return {"success": True, "page": detail}


@app.get("/api/fanpages/{page_id}/metrics")
async def get_fanpage_metrics_endpoint(page_id: str, days: int = 28):
    svc = get_fanpage_service()
    series = await asyncio.to_thread(svc.get_page_metrics, page_id, days=days)
    if series is None:
        raise HTTPException(status_code=404, detail="Fanpage không tồn tại")
    return {"success": True, **series}


@app.post("/api/fanpages/{page_id}/merge")
async def merge_fanpage_endpoint(page_id: str, req: FanpageMergeRequest):
    svc = get_fanpage_service()
    try:
        merged = await asyncio.to_thread(
            svc.merge_pages, page_id, req.source_page_id
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if merged is None:
        raise HTTPException(status_code=404, detail="Fanpage không tồn tại")
    return {"success": True, "page": merged}


@app.patch("/api/fanpages/{page_id}")
async def update_fanpage_endpoint(page_id: str, req: FanpageUpdateRequest):
    svc = get_fanpage_service()
    try:
        updated = await asyncio.to_thread(
            svc.update_page,
            page_id,
            tags=req.tags,
            note=req.note,
            preferred_profile_id=req.preferred_profile_id,
            is_archived=req.is_archived,
            expected_revision=req.expected_revision,
        )
    except FanpageRevisionConflictError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "code": "REVISION_CONFLICT",
                "detail": str(exc),
                "current_revision": exc.current_revision,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if updated is None:
        raise HTTPException(status_code=404, detail="Fanpage không tồn tại")
    return {"success": True, "page": updated}


@app.post("/api/fanpages/{page_id}/open")
async def open_fanpage_in_profile(
    page_id: str, req: Optional[FanpageOpenRequest] = None
):
    req = req or FanpageOpenRequest()
    svc = get_fanpage_service()
    detail = await asyncio.to_thread(svc.get_page, page_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Fanpage không tồn tại")

    linked_profiles = [
        lk
        for lk in (detail.get("linked_profiles") or [])
        if lk.get("link_status") != "profile_deleted"
        and not lk.get("profile_missing")
    ]
    linked_ids = {lk["profile_id"] for lk in linked_profiles}

    target_profile_id = (req.profile_id or "").strip() or None
    if target_profile_id:
        if target_profile_id not in linked_ids:
            raise HTTPException(
                status_code=400,
                detail="Profile được chọn không liên kết với fanpage này.",
            )
    else:
        preferred = detail.get("preferred_profile_id")
        if preferred and preferred in linked_ids:
            target_profile_id = preferred
        elif linked_profiles:
            active_links = [
                lk
                for lk in linked_profiles
                if lk.get("link_status") == "active"
            ]
            target_profile_id = (
                active_links[0]["profile_id"]
                if active_links
                else linked_profiles[0]["profile_id"]
            )

    if not target_profile_id:
        raise HTTPException(
            status_code=409,
            detail="Fanpage không còn profile khả dụng để mở.",
        )

    p = profile_manager.get_profile(target_profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")

    base_url = str(detail.get("canonical_url") or "").strip()
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="Fanpage chưa có URL hợp lệ (http/https) để mở.",
        )

    target_kind = (req.target or "page").strip().lower()
    if target_kind == "transparency":
        if "profile.php?id=" in base_url:
            open_url = f"{base_url}&sk=about_profile_transparency"
        else:
            open_url = f"{base_url.rstrip('/')}/about_profile_transparency"
    else:
        open_url = base_url

    gate = None
    gate_mode = _launch_gate_mode()
    if gate_mode != "off":
        try:
            already_running = await asyncio.to_thread(
                launcher.is_running, target_profile_id
            )
        except Exception:
            already_running = False
        if not already_running:
            profiles = await asyncio.to_thread(profile_manager.list_profiles)
            browser_paths = await asyncio.to_thread(
                _resolve_browser_paths, profiles
            )
            gate = await asyncio.to_thread(
                _evaluate_launch_gate,
                p,
                profiles,
                browser_paths,
                gate_mode,
                bool(req.force),
            )
            if not gate.allowed:
                return JSONResponse(
                    status_code=409,
                    content={
                        "success": False,
                        "detail": gate.message,
                        "gate": gate.as_dict(),
                    },
                )

    res = await asyncio.to_thread(
        launcher.launch, target_profile_id, open_url=open_url
    )
    if not res.get("success"):
        code = res.get("code")
        if code in _CDP_CONFLICT_CODES or res.get("recovery_required"):
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "code": code,
                    "detail": res.get("error", "Lỗi khởi chạy"),
                    "cdp_health": res.get("cdp_health"),
                },
            )
        raise HTTPException(
            status_code=500, detail=res.get("error", "Lỗi khởi chạy")
        )
    if gate and gate.status in ("warning", "overridden", "unverified"):
        res["gate"] = gate.as_dict()
    return {
        **res,
        "page_id": page_id,
        "profile_id": target_profile_id,
        "profile_name": p.name,
        "opened_url": open_url,
    }


# --- Fanpage Sync Jobs (Giai đoạn 2) ---


@app.post("/api/fanpage-sync/jobs", status_code=202)
async def create_fanpage_sync_job_endpoint(req: FanpageSyncJobCreateRequest):
    svc = get_fanpage_service()
    try:
        job, deduplicated = await asyncio.to_thread(
            svc.enqueue_sync_job,
            mode=req.mode,
            profile_ids=req.profile_ids,
            page_ids=req.page_ids,
            auto_start=req.auto_start,
            only_running=req.only_running,
            force_new=req.force_new,
            triggered_by="user",
            run_async=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        status_code=202,
        content={
            "success": True,
            "deduplicated": deduplicated,
            "job": job,
        },
    )


@app.get("/api/fanpage-sync/jobs")
async def list_fanpage_sync_jobs_endpoint(limit: int = 20):
    svc = get_fanpage_service()
    jobs = await asyncio.to_thread(svc.list_sync_jobs, limit=limit)
    active = await asyncio.to_thread(svc.repo.find_active_sync_job)
    return {
        "success": True,
        "active_job": active,
        "jobs": jobs,
    }


@app.get("/api/fanpage-sync/jobs/{job_id}")
async def get_fanpage_sync_job_endpoint(job_id: str):
    svc = get_fanpage_service()
    job = await asyncio.to_thread(svc.get_sync_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job đồng bộ không tồn tại.")
    return {"success": True, "job": job}


@app.post("/api/fanpage-sync/jobs/{job_id}/cancel")
async def cancel_fanpage_sync_job_endpoint(job_id: str):
    svc = get_fanpage_service()
    job = await asyncio.to_thread(svc.cancel_sync_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job đồng bộ không tồn tại.")
    return {"success": True, "job": job}


@app.post("/api/fanpage-sync/jobs/{job_id}/retry", status_code=202)
async def retry_fanpage_sync_job_endpoint(
    job_id: str, req: Optional[FanpageSyncJobRetryRequest] = None
):
    req = req or FanpageSyncJobRetryRequest()
    svc = get_fanpage_service()
    try:
        job, deduplicated = await asyncio.to_thread(
            svc.retry_sync_job,
            job_id,
            auto_start=req.auto_start,
            run_async=True,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job đồng bộ không tồn tại.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        status_code=202,
        content={
            "success": True,
            "deduplicated": deduplicated,
            "job": job,
        },
    )


# --- Fanpage Alerts & Operational Health (Giai đoạn 3) ---


@app.get("/api/fanpage-alerts")
async def list_fanpage_alerts_endpoint(
    status: str = "active",
    severity: str = "",
    alert_type: str = "",
    limit: int = 100,
):
    svc = get_fanpage_service()
    data = await asyncio.to_thread(
        svc.list_alerts,
        status=status,
        severity=severity,
        alert_type=alert_type,
        limit=limit,
    )
    return {"success": True, **data}


@app.post("/api/fanpage-alerts/acknowledge-all")
async def acknowledge_all_fanpage_alerts_endpoint():
    svc = get_fanpage_service()
    count = await asyncio.to_thread(svc.acknowledge_all_alerts)
    return {"success": True, "acknowledged_count": count}


@app.patch("/api/fanpage-alerts/{alert_id}")
async def update_fanpage_alert_endpoint(
    alert_id: int, req: FanpageAlertUpdateRequest
):
    svc = get_fanpage_service()
    try:
        alert = await asyncio.to_thread(
            svc.update_alert,
            alert_id,
            action=req.action,
            snooze_hours=req.snooze_hours,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if alert is None:
        raise HTTPException(status_code=404, detail="Cảnh báo không tồn tại.")
    return {"success": True, "alert": alert}
