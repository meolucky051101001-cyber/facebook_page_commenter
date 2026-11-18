"""
Bridge & CDP Automation Client for C:\\Projects\\Tools\\Antidetect Python.

Safety & Integrity Guarantees (per Antidetect Python HANDOFF.md):
1. NEVER writes to or modifies `data/profiles.json`, `secrets.dpapi`, or any profile
   metadata file inside `Antidetect Python`. All profile/fanpage discovery is 100% read-only.
2. Respects browser ownership: if a profile is already open in Antidetect Python,
   this module only opens a transient CDP tab (`/json/new?about%3Ablank`), performs
   the scan/comment, and closes the transient tab — NEVER closing the user's browser.
3. Uses DOM evidence to verify numeric Page and post identity before CDP input.
   Unknown layouts fail closed; uncertain submissions are never retried automatically.
"""
import asyncio
import csv
import json
import math
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from contextlib import contextmanager, closing

import config
import database as db
import meta_client
import browser_safety
import submit_control

_BRIDGE_LOCK = threading.RLock()
_INPROC_MANAGER = None
_INPROC_LAUNCHER = None
_PROFILE_OPERATION_LOCKS = {}
_OWNED_PROFILE_LEASES = {}
_PORT_BRIDGE_CONTEXT: Dict[int, Dict[str, Any]] = {}
_MAX_SUBMIT_EVIDENCE_AGE_SEC = 2.0


def _submit_time():
    return time.monotonic()


def _dispatch_paused():
    # Lazy import avoids a cycle; this predicate performs no I/O.
    import scheduler
    return scheduler.SCHEDULER_METRICS["paused_all"]


class BrowserAutomationError(RuntimeError):
    """A browser failure with an explicit dispatch category and connection status."""

    def __init__(self, message, category="INVALID_TARGET", browser_status="ERROR", retry_at=None):
        super().__init__(message)
        self.category = category
        self.browser_status = browser_status
        self.status = browser_status
        self.message = message
        self.retry_at = retry_at
        self.profile_safety_recorded = False
        self.safety_persistence_failed = False

    def as_result(self):
        return {"success": False, "comment_id": None, "category": self.category,
                "error": self.message, "browser_status": self.browser_status,
                "mode": "ANTIDETECT_CDP", "usage_pct": None, "retry_at": self.retry_at,
                "profile_safety_recorded": self.profile_safety_recorded,
                "safety_persistence_failed": self.safety_persistence_failed}


def get_antidetect_dir() -> Path:
    return Path(config.ANTIDETECT_DIR)


def is_antidetect_available() -> bool:
    ad_dir = get_antidetect_dir()
    return ad_dir.exists() and (ad_dir / "data" / "profiles.json").exists()


def _ensure_antidetect_sys_path() -> bool:
    ad_dir = str(get_antidetect_dir())
    if not os.path.isdir(ad_dir):
        return False
    if ad_dir not in sys.path:
        sys.path.insert(0, ad_dir)
    return True


def _read_profiles_readonly() -> List[Dict[str, Any]]:
    """Pure read-only load of data/profiles.json without touching file mtime or hash."""
    profiles_path = get_antidetect_dir() / "data" / "profiles.json"
    if not profiles_path.exists():
        return []
    with open(profiles_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def _read_runtime_readonly() -> Dict[str, Dict[str, Any]]:
    """Pure read-only load of data/browser_runtime.json."""
    rt_path = get_antidetect_dir() / "data" / "browser_runtime.json"
    if not rt_path.exists():
        return {}
    try:
        with open(rt_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _probe_cdp_port(port: Optional[int], timeout: float = 1.5) -> bool:
    if not port or int(port) <= 0:
        return False
    try:
        url = f"http://127.0.0.1:{int(port)}/json/version"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def list_antidetect_profiles() -> Dict[str, Any]:
    """
    Returns all profiles and their fanpages from Antidetect Python (read-only),
    annotated with live running status and CDP port.
    """
    if not is_antidetect_available():
        return {
            "available": False,
            "antidetect_dir": str(get_antidetect_dir()),
            "profiles": [],
            "total_profiles": 0,
            "total_fanpages": 0,
        }

    raw_profiles = _read_profiles_readonly()
    runtime_map = _read_runtime_readonly()
    profiles_out = []
    total_pages = 0

    for p in raw_profiles:
        pid = str(p.get("id") or "").strip()
        if not pid:
            continue
        rt = runtime_map.get(pid) or {}
        port = rt.get("port")
        if not port and _INPROC_LAUNCHER is not None:
            port = getattr(_INPROC_LAUNCHER, "active_ports", {}).get(pid)
        is_running = _probe_cdp_port(port)
        fb_pages = p.get("facebook_pages") or []
        total_pages += len(fb_pages)
        profiles_out.append({
            "id": pid,
            "name": p.get("name") or pid,
            "notes": p.get("notes") or "",
            "tags": p.get("tags") or [],
            "status": "running" if is_running else "stopped",
            "port": int(port) if (is_running and port) else None,
            "fanpages_count": len(fb_pages),
            "facebook_pages": fb_pages,
        })

    return {
        "available": True,
        "antidetect_dir": str(get_antidetect_dir()),
        "profiles": profiles_out,
        "total_profiles": len(profiles_out),
        "total_fanpages": total_pages,
    }


def sync_pages_from_antidetect(export_csv: bool = True) -> Dict[str, Any]:
    """
    Imports all profiles and Fanpages from Antidetect Python into fb_commenter.db.
    - Existing Graph API pages with matching names/IDs are upgraded to `HYBRID` mode
      (retaining their Graph API token while linking their Antidetect profile).
    - Pages without Graph API tokens are added in `ANTIDETECT` mode (`ANTIDETECT_READY`).
    """
    info = list_antidetect_profiles()
    if not info.get("available"):
        return {
            "success": False,
            "error": f"Không tìm thấy dữ liệu Antidetect tại: {info.get('antidetect_dir')}",
        }

    inserted = 0
    linked_hybrid = 0
    updated = 0
    profiles_synced = 0
    conflicts = []

    for prof in info["profiles"]:
        pid = prof["id"]
        pname = prof["name"]
        fb_pages = prof.get("facebook_pages") or []
        if not fb_pages:
            continue

        acc_id = db.upsert_account(
            account_name=pname,
            user_id=f"antidetect:{pid}",
            long_lived_user_token=None,
        )
        profiles_synced += 1

        for fp in fb_pages:
            fb_id = str(fp.get("id") or "").strip()
            fp_name = str(fp.get("name") or "").strip()
            fp_url = str(fp.get("url") or "").strip()
            if not fb_id and fp_url:
                m = re.search(r"facebook\.com/(?:profile\.php\?id=)?(\d{5,})", fp_url)
                if m:
                    fb_id = m.group(1)
            if not fb_id:
                continue
            if not fp_url:
                fp_url = f"https://www.facebook.com/{fb_id}"

            res = db.upsert_antidetect_page(
                fb_page_id=fb_id,
                page_name=fp_name or f"Page {fb_id}",
                page_url=fp_url,
                antidetect_profile_id=pid,
                antidetect_profile_name=pname,
                account_id=acc_id,
            )
            act = res.get("action")
            if act == "inserted":
                inserted += 1
            elif act == "linked_hybrid":
                linked_hybrid += 1
            elif act == "conflict":
                conflicts.append(fb_id)
            else:
                updated += 1

    if export_csv:
        try:
            export_all_pages_to_csv_templates()
        except Exception:
            pass

    all_pages = db.get_all_pages()
    return {
        "success": True,
        "profiles_synced": profiles_synced,
        "inserted": inserted,
        "linked_hybrid": linked_hybrid,
        "updated": updated,
        "conflicting_page_ids": sorted(set(conflicts)),
        "total_pages_in_db": len(all_pages),
    }


def export_all_pages_to_csv_templates() -> None:
    """Updates template_pages.csv, template_comments.csv, and template_gop_1_sheet_59_pages.csv with all pages in DB."""
    pages = db.get_all_pages()
    pages_sorted = sorted(pages, key=lambda x: (x.get("account_name") or "", x.get("page_name") or ""))
    base_dir = Path(__file__).resolve().parent

    pages_csv = base_dir / "template_pages.csv"
    comments_csv = base_dir / "template_comments.csv"
    unified_csv = base_dir / "template_gop_1_sheet_59_pages.csv"

    with open(pages_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["page_id", "page_name", "delay_minutes", "max_comments_per_day", "enabled", "note"])
        for p in pages_sorted:
            mode = p.get("connection_mode") or "GRAPH_API"
            prof_name = p.get("antidetect_profile_name") or p.get("account_name") or ""
            writer.writerow([
                f"'{p['page_id']}",
                p["page_name"],
                p.get("delay_minutes") or 10,
                p.get("max_comments_per_day") or 20,
                "TRUE" if p.get("rule_enabled", 1) else "FALSE",
                f"[{mode}] {prof_name}".strip(),
            ])

    with open(comments_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["page_id", "comment_template", "enabled", "weight", "tag"])
        for p in pages_sorted:
            templates = []
            try:
                templates = json.loads(p.get("templates") or "[]")
            except Exception:
                pass
            if not templates:
                templates = [
                    f"{{Dạ chào bạn|Chào bạn nhé|Xin chào bạn}}, cảm ơn bạn đã ghé thăm {p['page_name']}! {{Inbox page để được tư vấn chi tiết nhé|Nhắn tin cho tụi mình nếu cần hỗ trợ thêm nha}} ❤️"
                ]
            for idx, tmpl in enumerate(templates, start=1):
                writer.writerow([
                    f"'{p['page_id']}",
                    tmpl,
                    "TRUE",
                    1,
                    f"mau_{idx}",
                ])

    with open(unified_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Page ID",
            "Tên Fanpage",
            "Sau bao lâu comment (phút)",
            "Mỗi ngày mấy comment",
            "Mẫu bình luận",
            "Bật tự động",
            "Profile / Chế độ",
        ])
        for p in pages_sorted:
            mode = p.get("connection_mode") or "GRAPH_API"
            prof_name = p.get("antidetect_profile_name") or p.get("account_name") or ""
            templates = []
            try:
                templates = json.loads(p.get("templates") or "[]")
            except Exception:
                pass
            if not templates:
                templates = [
                    f"{{Dạ chào bạn|Chào bạn nhé|Xin chào bạn}}, cảm ơn bạn đã ghé thăm {p['page_name']}! {{Inbox page để được tư vấn chi tiết nhé|Nhắn tin cho tụi mình nếu cần hỗ trợ thêm nha}} ❤️"
                ]
            for tmpl in templates:
                writer.writerow([
                    f"'{p['page_id']}",
                    p["page_name"],
                    p.get("delay_minutes") or 10,
                    p.get("max_comments_per_day") or 20,
                    tmpl,
                    "TRUE" if p.get("rule_enabled", 1) else "FALSE",
                    f"[{mode}] {prof_name}".strip(),
                ])



# ============================================================================
# BROWSER LIFECYCLE & CDP PORT MANAGEMENT
# ============================================================================

def _get_running_antidetect_server() -> Optional[Tuple[int, str]]:
    """
    Checks if Antidetect Python's FastAPI server (`run.py`) is currently running.
    Returns `(server_port, access_key)` when healthy. A recorded but unhealthy
    server is an error: launching another owner would risk the user's profile.
    """
    ad_dir = get_antidetect_dir()
    pid_file = ad_dir / "data" / "server.pid"
    if not pid_file.exists():
        return None
    try:
        raw = pid_file.read_text(encoding="utf-8").strip()
        port = 8000
        if raw.startswith("{"):
            info = json.loads(raw)
            port = int(info.get("port") or 8000)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/health",
            headers={"User-Agent": "AntidetectHealthCheck"},
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status != 200:
                raise BrowserAutomationError("Máy chủ Antidetect có bản ghi nhưng chưa sẵn sàng.", "NETWORK_RETRYABLE")
            health = json.loads(resp.read().decode("utf-8"))
            if health.get("application") != "antidetect-python":
                raise BrowserAutomationError("Cổng máy chủ Antidetect đang do ứng dụng khác sử dụng.", "NETWORK_RETRYABLE")

        try:
            if _ensure_antidetect_sys_path():
                from antidetect.runtime_auth import read_runtime
                rec = read_runtime() or {}
                access_key = rec.get("access_key")
                if access_key:
                    return port, str(access_key)
        except Exception as exc:
            raise BrowserAutomationError("Máy chủ Antidetect đang chạy nhưng không đọc được quyền truy cập.", "NETWORK_RETRYABLE") from exc
        raise BrowserAutomationError("Máy chủ Antidetect đang chạy nhưng thiếu quyền truy cập.", "NETWORK_RETRYABLE")
    except BrowserAutomationError:
        raise
    except Exception as exc:
        raise BrowserAutomationError("Có bản ghi máy chủ Antidetect nhưng chưa xác minh được trạng thái. Hãy mở lại Antidetect rồi thử lại.", "NETWORK_RETRYABLE") from exc


def _remember_owned_launch(profile_id, port, result):
    """Only a positive launch acknowledgement establishes ownership."""
    if result.get("launched_now") is not True:
        return False
    health = result.get("cdp_health") or {}
    launch_id = result.get("launch_id") or health.get("launch_id")
    owner = result.get("owner_instance") or health.get("owner_instance")
    if launch_id:
        _OWNED_PROFILE_LEASES[profile_id] = {
            "port": int(port), "launch_id": str(launch_id),
            "owner_instance": str(owner) if owner else None,
        }
    # Without an immutable launch id the tab is usable, but the browser must
    # remain open because cleanup cannot prove ownership of its current session.
    return bool(launch_id)


def _get_inproc_launcher():
    """Lazy-initializes an in-process BrowserLauncher singleton when Antidetect server is not running."""
    global _INPROC_MANAGER, _INPROC_LAUNCHER
    with _BRIDGE_LOCK:
        if _INPROC_LAUNCHER is not None:
            return _INPROC_MANAGER, _INPROC_LAUNCHER
        if not _ensure_antidetect_sys_path():
            raise RuntimeError("Không tìm thấy thư mục Antidetect Python.")
        from antidetect.storage import ProfileManager
        from antidetect.launcher import BrowserLauncher
        _INPROC_MANAGER = ProfileManager()
        _INPROC_LAUNCHER = BrowserLauncher(_INPROC_MANAGER)
        return _INPROC_MANAGER, _INPROC_LAUNCHER


def _runtime_record_is_healthy(rt: Dict[str, Any]) -> bool:
    """Ensure a persisted runtime record has not degraded or detached its stealth session."""
    if not isinstance(rt, dict):
        return False
    if rt.get("detached") is True or rt.get("cleanup_failed") is True:
        return False
    health = rt.get("cdp_health")
    if isinstance(health, dict) and health.get("state") not in (None, "healthy"):
        return False
    return True


def _launch_result_is_healthy(res: Any) -> bool:
    """Verify Antidetect confirmed stealth/CDP readiness (not merely browser process start)."""
    if not isinstance(res, dict) or res.get("success") is not True:
        return False
    if res.get("degraded") is True or res.get("cdp_ready") is False:
        return False
    health = res.get("cdp_health")
    if isinstance(health, dict) and health.get("state") not in (None, "healthy"):
        return False
    return True


def _remember_port_context(port: int, profile_id: str, srv_port: Optional[int]) -> None:
    with _BRIDGE_LOCK:
        _PORT_BRIDGE_CONTEXT[int(port)] = {
            "profile_id": str(profile_id),
            "server_port": int(srv_port) if srv_port is not None else None,
        }


def _get_port_context(port: int) -> Dict[str, Any]:
    with _BRIDGE_LOCK:
        ctx = _PORT_BRIDGE_CONTEXT.get(int(port))
        return dict(ctx) if isinstance(ctx, dict) else {}


def _update_port_server(port: int, srv_port: int) -> None:
    with _BRIDGE_LOCK:
        if int(port) in _PORT_BRIDGE_CONTEXT and isinstance(_PORT_BRIDGE_CONTEXT[int(port)], dict):
            _PORT_BRIDGE_CONTEXT[int(port)]["server_port"] = int(srv_port)


def _forget_port_context(port: Optional[int]) -> None:
    if port is None:
        return
    with _BRIDGE_LOCK:
        _PORT_BRIDGE_CONTEXT.pop(int(port), None)


def acquire_profile_cdp_port(profile_id: str) -> Tuple[int, bool, Optional[int]]:
    """
    Ensures the specified Antidetect profile is running with healthy stealth CDP and returns:
      `(cdp_port, opened_by_us, server_port_if_started_via_server)`
    If the profile was already running before this call, `opened_by_us` is False
    so the caller will NOT stop the browser after finishing.
    """
    with _BRIDGE_LOCK:
        # 1. Check if already running in browser_runtime.json
        rt = _read_runtime_readonly().get(profile_id) or {}
        existing_port = rt.get("port")
        if _probe_cdp_port(existing_port):
            if not _runtime_record_is_healthy(rt):
                raise BrowserAutomationError(
                    "Profile đang mở nhưng kênh CDP stealth không khỏe hoặc chạy detached; hãy dừng và mở lại profile trong Antidetect.",
                    "NETWORK_RETRYABLE",
                )
            _remember_port_context(int(existing_port), profile_id, None)
            return int(existing_port), False, None

        # 2. Check if already running in our in-process launcher
        if _INPROC_LAUNCHER is not None:
            inproc_port = getattr(_INPROC_LAUNCHER, "active_ports", {}).get(profile_id)
            if _probe_cdp_port(inproc_port):
                cdp_guard = getattr(_INPROC_LAUNCHER, "cdp_guard", None)
                if callable(cdp_guard):
                    guard = cdp_guard(profile_id, running=True)
                    if isinstance(guard, dict) and not guard.get("ok", True):
                        raise BrowserAutomationError(
                            "Profile trong tiến trình hiện tại có kênh CDP không khỏe; hãy mở lại profile.",
                            "NETWORK_RETRYABLE",
                        )
                _remember_port_context(int(inproc_port), profile_id, None)
                return int(inproc_port), False, None

        # 3. Try starting via running Antidetect FastAPI server first
        srv = _get_running_antidetect_server()
        if srv is not None:
            srv_port, access_key = srv
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{srv_port}/api/profiles/{urllib.parse.quote(profile_id, safe='')}/start?require_healthy=true",
                    data=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {access_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=25.0) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    port = res.get("port")
                    if _launch_result_is_healthy(res) and _probe_cdp_port(port):
                        _remember_port_context(int(port), profile_id, srv_port)
                        return int(port), _remember_owned_launch(profile_id, port, res), srv_port
                    raise BrowserAutomationError("Antidetect đã mở profile nhưng stealth/CDP chưa được xác minh khỏe.", "NETWORK_RETRYABLE")
            except BrowserAutomationError:
                raise
            except Exception as exc:
                raise BrowserAutomationError("Máy chủ Antidetect từ chối hoặc không hoàn tất mở profile khỏe.", "NETWORK_RETRYABLE") from exc

        # 4. Launch in-process via BrowserLauncher
        _, launcher = _get_inproc_launcher()
        if launcher.is_running(profile_id):
            port = launcher.active_ports.get(profile_id)
            if _probe_cdp_port(port):
                cdp_guard = getattr(launcher, "cdp_guard", None)
                if callable(cdp_guard):
                    guard = cdp_guard(profile_id, running=True)
                    if isinstance(guard, dict) and not guard.get("ok", True):
                        raise BrowserAutomationError(
                            "Profile đang mở nhưng kênh CDP stealth không khỏe; hãy dừng và mở lại profile.",
                            "NETWORK_RETRYABLE",
                        )
                _remember_port_context(int(port), profile_id, None)
                return int(port), False, None

        res = launcher.launch(profile_id)
        if not res or not res.get("success"):
            err = (res or {}).get("error") or "Không thể khởi động profile Antidetect."
            raise RuntimeError(err)
        if not _launch_result_is_healthy(res):
            if res.get("launched_now") is True:
                launch_id = res.get("launch_id") or (res.get("cdp_health") or {}).get("launch_id")
                try:
                    launcher.stop(
                        profile_id,
                        expected_launch_id=launch_id,
                        expected_owner_instance=getattr(launcher, "instance_id", None),
                    )
                except Exception:
                    pass
            raise BrowserAutomationError(
                "Profile đã mở nhưng stealth/fingerprint chưa xác minh thành công; đã dừng để bảo vệ tài khoản.",
                "NETWORK_RETRYABLE",
            )
        port = res.get("port") or launcher.active_ports.get(profile_id)
        if not _probe_cdp_port(port):
            raise RuntimeError(f"Profile {profile_id} đã mở nhưng cổng CDP ({port}) không phản hồi.")
        _remember_port_context(int(port), profile_id, None)
        return int(port), _remember_owned_launch(profile_id, port, res), None


def release_profile_cdp_port(profile_id: str, opened_by_us: bool, srv_port: Optional[int] = None) -> None:
    """
    Stops the profile ONLY if `opened_by_us` is True and `config.ANTIDETECT_AUTO_CLOSE_BROWSER` is True.
    Never closes a profile that the user had opened manually!
    """
    if not opened_by_us:
        return
    with _BRIDGE_LOCK:
        lease = _OWNED_PROFILE_LEASES.pop(profile_id, None)
        if not lease or not config.ANTIDETECT_AUTO_CLOSE_BROWSER:
            return
        current = _read_runtime_readonly().get(profile_id) or {}
        if current.get("launch_id") != lease["launch_id"] or current.get("port") != lease["port"]:
            return
        if lease["owner_instance"] and current.get("owner_instance") != lease["owner_instance"]:
            return
        if srv_port is not None:
            try:
                srv = _get_running_antidetect_server()
            except Exception:
                srv = None
            if srv is not None and srv[0] == srv_port:
                _, access_key = srv
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{srv_port}/api/profiles/{urllib.parse.quote(profile_id, safe='')}/stop/owned?" +
                        urllib.parse.urlencode({"expected_launch_id": lease["launch_id"]}),
                        data=b"{}",
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {access_key}",
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=10.0):
                        _forget_port_context(lease["port"])
                        return
                except Exception:
                    pass
            # This browser belongs to the server, never stop an unrelated launcher.
            return
        if _INPROC_LAUNCHER is not None:
            try:
                _INPROC_LAUNCHER.stop(profile_id, expected_launch_id=lease["launch_id"],
                                      expected_owner_instance=lease["owner_instance"])
                _forget_port_context(lease["port"])
            except Exception:
                pass


# ============================================================================
# CDP WEBSOCKET HELPERS & TRANSIENT TAB AUTOMATION
# ============================================================================

def _cdp_http_json(port: int, path: str, method: str = "GET", timeout: float = 5.0) -> Any:
    url = f"http://127.0.0.1:{int(port)}{path}"
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}


async def _verify_blank_tab_stealth(ws_url: str, timeout: float = 5.0) -> bool:
    """Confirm the newly opened about:blank tab is unpaused and stealth-injected before navigation."""
    import websockets

    deadline = time.monotonic() + max(0.5, float(timeout))
    cmd_id = 88000
    while time.monotonic() < deadline:
        try:
            rem = max(0.2, deadline - time.monotonic())
            async with websockets.connect(ws_url, open_timeout=min(2.0, rem), max_size=2 * 1024 * 1024) as ws:
                cmd_id += 1
                val = await _cdp_eval(
                    ws,
                    cmd_id,
                    "({wd: navigator.webdriver, href: location.href})",
                    timeout=min(1.5, max(0.3, deadline - time.monotonic())),
                )
                if isinstance(val, dict) and val.get("wd") is False:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.15)
    return False


def _open_transient_tab(port: int) -> Tuple[str, str]:
    """Opens a temporary blank tab with verified stealth injection and returns `(target_id, ws_url)`."""
    ctx = _get_port_context(port)
    profile_id = ctx.get("profile_id")
    srv_port = ctx.get("server_port")

    # 1. Prefer Antidetect server's synchronized /tabs/transient endpoint when available.
    if profile_id:
        resolved_srv = None
        if srv_port is not None:
            try:
                srv = _get_running_antidetect_server()
                if srv is not None and srv[0] == srv_port:
                    resolved_srv = srv
            except BrowserAutomationError:
                raise
            except Exception:
                resolved_srv = None
        else:
            try:
                resolved_srv = _get_running_antidetect_server()
            except Exception:
                resolved_srv = None
        if resolved_srv is not None:
            s_port, access_key = resolved_srv
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{s_port}/api/profiles/{urllib.parse.quote(profile_id, safe='')}/tabs/transient",
                    data=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {access_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    tab_res = json.loads(resp.read().decode("utf-8"))
                    t_id = str((tab_res or {}).get("target_id") or "").strip()
                    w_url = str((tab_res or {}).get("ws_url") or "").strip()
                    if t_id and w_url:
                        _update_port_server(port, s_port)
                        return t_id, w_url
                    raise BrowserAutomationError(
                        "Máy chủ Antidetect trả phản hồi mở tab tạm không đầy đủ.",
                        "NETWORK_RETRYABLE",
                    )
            except urllib.error.HTTPError as http_exc:
                if http_exc.code == 409:
                    raise BrowserAutomationError(
                        "Kênh CDP stealth của profile không khỏe; đã chặn mở tab Facebook.",
                        "NETWORK_RETRYABLE",
                    ) from http_exc
                if http_exc.code not in (404, 405):
                    raise BrowserAutomationError(
                        "Không tạo được tab tạm an toàn qua máy chủ Antidetect.",
                        "NETWORK_RETRYABLE",
                    ) from http_exc
            except BrowserAutomationError:
                raise
            except Exception as exc:
                raise BrowserAutomationError(
                    "Máy chủ Antidetect không xác nhận mở tab tạm đúng hạn; đã hủy để tránh tạo tab mồ côi.",
                    "NETWORK_RETRYABLE",
                ) from exc

    # 2. Prefer in-process launcher's synchronized open_transient_tab when available.
    if profile_id and _INPROC_LAUNCHER is not None and hasattr(_INPROC_LAUNCHER, "open_transient_tab"):
        tab_res = _INPROC_LAUNCHER.open_transient_tab(profile_id, timeout=8.0)
        t_id = str((tab_res or {}).get("target_id") or "").strip()
        w_url = str((tab_res or {}).get("ws_url") or "").strip()
        if t_id and w_url:
            return t_id, w_url

    # 3. Fallback: open /json/new?about:blank and verify stealth before returning.
    try:
        info = _cdp_http_json(port, "/json/new?about%3Ablank", method="PUT")
    except Exception:
        info = _cdp_http_json(port, "/json/new?about%3Ablank", method="GET")
    target_id = str((info or {}).get("id") or "").strip()
    if not target_id:
        raise RuntimeError("Chrome CDP không trả về target_id khi mở tab tạm.")

    if _ensure_antidetect_sys_path():
        try:
            from antidetect.facebook import TRANSIENT_TARGET_IDS
            TRANSIENT_TARGET_IDS.add(target_id)
        except Exception:
            pass

    ws_url = (info or {}).get("webSocketDebuggerUrl")
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and not ws_url:
        try:
            targets = _cdp_http_json(port, "/json/list")
            for t in targets or []:
                if t.get("id") == target_id and t.get("webSocketDebuggerUrl"):
                    ws_url = t["webSocketDebuggerUrl"]
                    break
        except Exception:
            pass
        if not ws_url:
            time.sleep(0.25)

    if not ws_url:
        _close_transient_tab(port, target_id)
        raise RuntimeError("Không lấy được webSocketDebuggerUrl cho tab CDP tạm.")

    # Wait for stealth configuration (navigator.webdriver === false) before allowing Page.navigate.
    try:
        try:
            asyncio.get_running_loop()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                stealth_ok = pool.submit(asyncio.run, _verify_blank_tab_stealth(str(ws_url), 5.0)).result()
        except RuntimeError:
            stealth_ok = asyncio.run(_verify_blank_tab_stealth(str(ws_url), 5.0))
    except Exception:
        stealth_ok = False
    if not stealth_ok:
        _close_transient_tab(port, target_id)
        raise BrowserAutomationError(
            "Tab CDP tạm chưa xác nhận tiêm stealth (navigator.webdriver); đã đóng tab trước khi mở Facebook.",
            "NETWORK_RETRYABLE",
        )
    return target_id, str(ws_url)


def _close_transient_tab(port: int, target_id: str) -> None:
    if not target_id:
        return
    ctx = _get_port_context(port)
    profile_id = ctx.get("profile_id")
    srv_port = ctx.get("server_port")
    if profile_id and srv_port is not None:
        try:
            srv = _get_running_antidetect_server()
            if srv is not None and srv[0] == srv_port:
                _, access_key = srv
                req = urllib.request.Request(
                    f"http://127.0.0.1:{srv_port}/api/profiles/{urllib.parse.quote(profile_id, safe='')}/tabs/transient/{urllib.parse.quote(str(target_id), safe='')}",
                    headers={"Authorization": f"Bearer {access_key}"},
                    method="DELETE",
                )
                with urllib.request.urlopen(req, timeout=3.0):
                    return
        except Exception:
            pass
    if profile_id and _INPROC_LAUNCHER is not None and hasattr(_INPROC_LAUNCHER, "close_transient_tab"):
        try:
            if _INPROC_LAUNCHER.close_transient_tab(profile_id, str(target_id)):
                return
        except Exception:
            pass
    if _ensure_antidetect_sys_path():
        try:
            from antidetect.facebook import TRANSIENT_TARGET_IDS
            TRANSIENT_TARGET_IDS.discard(str(target_id))
        except Exception:
            pass
    for method in ("PUT", "GET"):
        try:
            _cdp_http_json(port, f"/json/close/{target_id}", method=method, timeout=3.0)
            return
        except Exception:
            pass


async def _cdp_call(ws, msg_id: int, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Dict[str, Any]:
    payload = {"id": msg_id, "method": method, "params": params or {}}
    await ws.send(json.dumps(payload))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rem = max(0.2, deadline - time.monotonic())
        raw = await asyncio.wait_for(ws.recv(), timeout=rem)
        msg = json.loads(raw)
        if msg.get("id") == msg_id:
            if msg.get("error"):
                raise RuntimeError(f"CDP {method} failed: {msg['error'].get('message', 'protocol error')}")
            return msg
    raise TimeoutError(f"CDP call {method} timed out after {timeout}s")


async def _cdp_eval(ws, msg_id: int, expression: str, timeout: float = 10.0) -> Any:
    res = await _cdp_call(
        ws,
        msg_id,
        "Runtime.evaluate",
        {"expression": expression, "awaitPromise": True, "returnByValue": True},
        timeout=timeout,
    )
    if res.get("error"):
        return None
    return ((res.get("result") or {}).get("result") or {}).get("value")


# DOM evidence is restricted to rendered controls and the selected post. No
# cookies, credentials, private endpoints, or application internals are read.
_BROWSER_ACCESS_STATE_JS = r"""
(() => {
  if (/^\/(checkpoint|two_step_verification|recover)(\/|$)/i.test(location.pathname)) return 'checkpoint';
  if (/^\/(login|login\.php)(\/|$)/i.test(location.pathname) ||
      (document.querySelector('input[name="email"]') && document.querySelector('input[name="pass"]'))) return 'login_required';
  // Inspect only rendered system notices. A post, comment, or draft quoting a
  // warning must never be treated as Facebook restricting this session.
  for (const el of document.querySelectorAll('[role="alert"],[role="alertdialog"],[role="dialog"],[role="status"]')) {
    if (el.closest('[role="article"],[contenteditable="true"]') ||
        el.querySelector('[role="article"],[contenteditable="true"]')) continue;
    const r = el.getBoundingClientRect(), style = getComputedStyle(el);
    if (r.width <= 2 || r.height <= 2 || style.visibility === 'hidden' || style.display === 'none') continue;
    const text = String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (/confirm your identity|xác nhận danh tính của bạn|security check|kiểm tra bảo mật/i.test(text)) return 'checkpoint';
    if (/you(?:'|’)?re temporarily blocked|you are temporarily blocked|we limit how often|we restrict (?:certain|some) activit(?:y|ies) to protect (?:our|the) community|you (?:can(?:not|'t|’t)) use this feature right now|your account (?:is|has been) restricted|you (?:can(?:not|'t|’t)) comment|bạn (?:tạm thời bị chặn|đang bị chặn tạm thời|không thể bình luận)|chúng tôi giới hạn (?:tần suất|mức độ thường xuyên)|chúng tôi hạn chế một số hoạt động (?:để|nhằm) bảo vệ cộng đồng|(?:hiện )?bạn không thể (?:sử dụng|dùng) tính năng này|tài khoản của bạn (?:đang |đã )?bị hạn chế/i.test(text)) return 'restricted';
  }
  return 'ok';
})
"""

_BROWSER_READINESS_JS = r"""
(() => { const access = __ACCESS_STATE__(); return {url: location.href, readyState: document.readyState,
  mainPresent: Boolean(document.querySelector('[role="main"]')),
  loginPresent: access === 'login_required', checkpointPresent: access === 'checkpoint',
  restrictedPresent: access === 'restricted'
}; })()
""".replace('__ACCESS_STATE__', _BROWSER_ACCESS_STATE_JS)

_BROWSER_EVIDENCE_JS = r"""
((expectedPost, message) => {
  const out = {url: location.href, accessState: __ACCESS_STATE__(), actorIds: [],
    actorOptions: [], actorButtonRect: null, posts: [], comments: [],
    commentIds: [], commentBoxRect: null, commentBoxText: '', focused: false};
  if (out.accessState !== 'ok') return out;
  const num = x => /^\d+$/.test(String(x || '')) ? String(x) : '';
  const pfb = x => /^pfbid[A-Za-z0-9]{10,120}$/.test(String(x || '')) ? String(x) : '';
  const tok = x => num(x) || pfb(x);
  const norm = s => String(s || '').replace(/\r\n/g, '\n').replace(/\s+/g, ' ').trim();
  const visible = el => { const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2 && getComputedStyle(el).visibility !== 'hidden'; };
  const rect = el => { if (!el || !visible(el)) return null;
    const r = el.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2}; };
  const url = s => { try { const u = new URL(s, location.href);
    if (u.protocol !== 'https:' || !['facebook.com','www.facebook.com','m.facebook.com'].includes(u.hostname)
        || u.username || u.password || (u.port && u.port !== '443')) return null;
    return u;
  } catch (_) { return null; } };
  const profile = s => { const u = url(s); if (!u) return '';
    if (/^\/(?:profile\.php)?\/?$/.test(u.pathname)) return num(u.searchParams.get('id'));
    const match = /^\/(?:people\/[^/]+\/|pages\/[^/]+\/)?(\d+)\/?$/.exec(u.pathname);
    return match ? match[1] : '';
  };
  const post = s => { const u = url(s); if (!u) return null;
    if (u.searchParams.has('comment_id') || u.searchParams.has('reply_comment_id')) return null;
    const m = /^\/(\d+|[^/]+)\/posts\/(\d+|pfbid[A-Za-z0-9]{10,120})\/?$/.exec(u.pathname);
    const story = tok(u.searchParams.get('story_fbid'));
    const v = /^\/(?:[^/]+\/)?(?:videos|reel)\/(\d+)\/?$/.exec(u.pathname);
    const rawId = story || (m && m[2]) || (v && v[1]);
    if (!rawId) return null;
    return {id: num(rawId) || rawId, pfbid: pfb(rawId), owner: story ? num(u.searchParams.get('id')) : (m ? num(m[1]) : ''), url: u.href};
  };
  const relayById = new Map(), relayByPfbid = new Map(), relayCommentAuthors = new Map(), sessionActors = new Set();
  const mergeRelay = (numId, pfbToken, ownerId, ctime) => {
    const apply = (map, key) => {
      if (!key) return;
      const cur = map.get(key) || {id: '', pfbid: '', owners: new Set(), time: null, timeConflict: false};
      if (numId) { if (cur.id && cur.id !== numId) cur.idConflict = true; else cur.id = numId; }
      if (pfbToken) { if (cur.pfbid && cur.pfbid !== pfbToken) cur.idConflict = true; else cur.pfbid = pfbToken; }
      if (ownerId) cur.owners.add(ownerId);
      if (ctime) { if (cur.time && cur.time !== ctime) cur.timeConflict = true; else cur.time = ctime; }
      map.set(key, cur);
    };
    apply(relayById, numId);
    apply(relayByPfbid, pfbToken);
  };
  const unixSec = v => {
    const n = typeof v === 'number' ? v : (/^\d{10,13}$/.test(String(v || '')) ? Number(v) : NaN);
    if (!Number.isFinite(n) || n <= 1072915200) return null;
    return String(n > 1e11 ? Math.floor(n / 1000) : Math.floor(n));
  };
  const inspectRelayTree = (root, budget) => {
    const stack = [root];
    while (stack.length && budget.left > 0) {
      const node = stack.pop();
      if (!node || typeof node !== 'object') continue;
      budget.left--;
      if (Array.isArray(node)) {
        for (let i = node.length - 1; i >= 0; i--) stack.push(node[i]);
        continue;
      }
      if (node.ACCOUNT_ID && ('USER_ID' in node || 'APP_ID' in node)) {
        const aid = num(node.ACCOUNT_ID);
        if (aid && aid !== '0') sessionActors.add(aid);
      }
      const cLegacy = num(node.legacy_fbid || node.comment_id);
      const cAuthor = num(node.author && node.author.id);
      if (cLegacy && cAuthor && (node.__typename === 'Comment' || node.body)) {
        const set = relayCommentAuthors.get(cLegacy) || new Set();
        set.add(cAuthor);
        relayCommentAuthors.set(cLegacy, set);
      }
      const numId = num(node.post_id) || (node.__typename === 'Video' ? num(node.id) : '');
      let pfbToken = '', urlOwner = '';
      for (const cand of [node.url, node.wwwURL, node.permalink_url]) {
        if (typeof cand !== 'string' || !cand) continue;
        const parsed = post(cand);
        if (parsed) {
          if (parsed.pfbid) pfbToken = pfbToken || parsed.pfbid;
          if (parsed.owner) urlOwner = urlOwner || parsed.owner;
        }
      }
      const ownerId = num(node.owning_profile && node.owning_profile.id) ||
        (Array.isArray(node.actors) && node.actors.length === 1 ? num(node.actors[0] && node.actors[0].id) : '') || urlOwner;
      const ctime = unixSec(node.creation_time) || unixSec(node.publish_time);
      if ((numId || pfbToken) && (ctime || ownerId || (numId && pfbToken))) {
        mergeRelay(numId, pfbToken, ownerId, ctime);
      }
      for (const val of Object.values(node)) {
        if (val && typeof val === 'object') stack.push(val);
      }
    }
  };
  try {
    const scripts = document.querySelectorAll ? document.querySelectorAll('script[type="application/json"]') : [];
    const budget = {left: 18000};
    let count = 0;
    for (const sc of scripts) {
      if (++count > 120 || budget.left <= 0) break;
      const raw = String(sc.textContent || '');
      if (!raw || raw.length > 1500000) continue;
      if (!/creation_time|publish_time|post_id|pfbid|ACCOUNT_ID|legacy_fbid/.test(raw)) continue;
      try { inspectRelayTree(JSON.parse(raw), budget); } catch (_) {}
    }
  } catch (_) {}
  const fiberChain = (el, boundary, maxSteps = 24) => {
    const propsList = [];
    try {
      if (!el || typeof el !== 'object') return propsList;
      const key = Object.keys(el).find(k => k.startsWith('__reactFiber$') || k.startsWith('__reactProps$'));
      if (!key) return propsList;
      let cur = el[key];
      if (key.startsWith('__reactProps$')) return cur && typeof cur === 'object' ? [cur] : [];
      for (let i = 0; cur && i < maxSteps; i++, cur = cur.return) {
        if (boundary && cur.stateNode && cur.stateNode instanceof Element && !boundary.contains(cur.stateNode)) break;
        const p = cur.memoizedProps || cur.pendingProps;
        if (p && typeof p === 'object') propsList.push(p);
      }
    } catch (_) {}
    return propsList;
  };
  const fiberActorIds = (el, boundary) => {
    const ids = new Set();
    const directKeys = ['actorID','actor_id','selectedActorID','currentActorID','voiceActorID','profileID','profile_id','pageID','page_id'];
    for (const p of fiberChain(el, boundary, 16)) {
      for (const k of directKeys) { const v = num(p[k]); if (v && v !== '0') ids.add(v); }
      for (const objKey of ['actor','currentActor','selectedActor','voiceActor','delegate_page','profile']) {
        const sub = p[objKey];
        if (sub && typeof sub === 'object') {
          const v = num(sub.id || sub.actorID || sub.profile_id);
          if (v && v !== '0') ids.add(v);
        }
      }
    }
    return [...ids];
  };
  const fiberArticleStory = art => {
    const budget = {left: 800};
    for (const p of fiberChain(art, art, 24)) {
      for (const k of ['story','post','feedback','video']) {
        if (p[k] && typeof p[k] === 'object') inspectRelayTree(p[k], budget);
      }
      if (p.creation_time || p.publish_time || p.post_id) inspectRelayTree(p, budget);
    }
  };
  const identity = el => { const ids = new Set();
    for (const n of [el, ...(el.querySelectorAll ? el.querySelectorAll('[data-actor-id],[data-profile-id],a[href]') : [])]) {
      const id = num(n.getAttribute && n.getAttribute('data-actor-id')) || num(n.getAttribute && n.getAttribute('data-profile-id')) ||
        (n.matches && n.matches('a[href]') && profile(n.href));
      if (id) ids.add(id);
    }
    return [...ids];
  };
  const ownLinks = art => [...art.querySelectorAll('a[href]')].filter(a => a.closest('[role="article"]') === art);
  const targets = new Set();
  const targetTokens = new Map();
  for (const art of document.querySelectorAll('[role="article"]')) {
    if (art.parentElement.closest('[role="article"]')) continue;
    fiberArticleStory(art);
    const links = ownLinks(art);
    for (const a of links) {
      const p = post(a.href); if (!p) continue;
      const rInfo = (p.pfbid && relayByPfbid.get(p.pfbid)) || (num(p.id) && relayById.get(p.id)) || null;
      const validRelay = rInfo && !rInfo.idConflict ? rInfo : null;
      const canonicalId = (validRelay && validRelay.id) || p.id;
      const pfbToken = p.pfbid || (validRelay && validRelay.pfbid) || '';
      const owners = new Set(p.owner ? [p.owner] : []);
      if (validRelay) for (const oid of validRelay.owners) owners.add(oid);
      const pageAttr = num(art.getAttribute('data-page-id')); if (pageAttr) owners.add(pageAttr);
      for (const head of art.querySelectorAll('h2 a[href],h3 a[href]')) {
        if (head.closest('[role="article"]') !== art) continue;
        const id = profile(head.href); if (id) owners.add(id);
      }
      const timeEl = a.matches('time[datetime],abbr[data-utime]') ? a :
        a.querySelector('time[datetime],abbr[data-utime]');
      const domTime = a.getAttribute('data-utime') || (timeEl &&
        (timeEl.getAttribute('data-utime') || timeEl.getAttribute('datetime')));
      const relayTime = validRelay && !validRelay.timeConflict ? validRelay.time : null;
      const absoluteTime = domTime || relayTime;
      const entry = {id: canonicalId, ownerIds: [...owners], url: p.url, created_time: absoluteTime || null};
      if (pfbToken) entry.pfbid = pfbToken;
      out.posts.push(entry);
      if (expectedPost && (canonicalId === expectedPost || pfbToken === expectedPost || p.id === expectedPost)) {
        targets.add(art);
        const set = targetTokens.get(art) || new Set();
        set.add(canonicalId);
        if (pfbToken) set.add(pfbToken);
        targetTokens.set(art, set);
      }
    }
  }
  // A composer must belong to exactly this post, not a suggested post or reply.
  const target = targets.size === 1 ? [...targets][0] : null;
  const acceptedPostTokens = target ? (targetTokens.get(target) || new Set([expectedPost])) : new Set(expectedPost ? [expectedPost] : []);
  let box = null;
  if (target) {
    const boxes = [...target.querySelectorAll('[contenteditable="true"][role="textbox"]')].filter(b => {
      const label = b.getAttribute('aria-label') || b.getAttribute('aria-placeholder') || '';
      return visible(b) && b.closest('[role="article"]') === target && /bình luận|comment/i.test(label) &&
        !/trả lời|reply/i.test(label);
    });
    if (boxes.length === 1) box = boxes[0];
  }
  const scope = target || document.querySelector('[role="main"]') || document;
  // Only explicitly labelled actor controls contribute identities first.
  // Author links and generic selected menu entries never identify the sender.
  const marker = /đang tương tác dưới tên|bình luận (dưới tên|với tư cách)|chuyển (trang cá nhân|profile)|commenting as|comment as|interacting as|switch profile/i;
  const actorIds = new Set();
  for (const control of scope.querySelectorAll('[role="button"],button,[role="status"]')) {
    if (!visible(control)) continue;
    const label = control.getAttribute('aria-label') || control.textContent || '';
    if (!marker.test(label)) continue;
    const domIds = identity(control);
    const resolvedIds = domIds.length ? domIds : fiberActorIds(control, control);
    for (const id of resolvedIds) actorIds.add(id);
    if (!out.actorButtonRect) out.actorButtonRect = rect(control);
  }
  if (box) {
    for (const attr of ['data-actor-id','data-profile-id']) {
      const id = num(box.getAttribute(attr)); if (id) actorIds.add(id);
    }
    if (!actorIds.size) {
      for (const id of fiberActorIds(box, target || box)) actorIds.add(id);
    }
    out.commentBoxRect = rect(box); out.commentBoxText = box.innerText || '';
    out.focused = document.activeElement === box || box.contains(document.activeElement);
  }
  if (!actorIds.size && !out.actorButtonRect && sessionActors.size === 1) {
    for (const id of sessionActors) actorIds.add(id);
  }
  for (const option of document.querySelectorAll('[role="menuitem"],[role="menuitemradio"],[role="option"],[role="radio"]')) {
    if (!visible(option)) continue;
    const domIds = identity(option);
    const ids = domIds.length ? domIds : fiberActorIds(option, option);
    if (ids.length !== 1) continue;
    out.actorOptions.push({id:ids[0], ...rect(option)});
  }
  out.actorIds = [...actorIds];
  if (target) for (const link of target.querySelectorAll('a[href]')) {
    const u = url(link.href); if (!u) continue;
    const cid = num(u.searchParams.get('comment_id'));
    if (!cid || u.searchParams.has('reply_comment_id')) continue;
    const parentURL = new URL(u.href);
    parentURL.searchParams.delete('comment_id');
    const parentPost = post(parentURL.href);
    if (!parentPost || (!acceptedPostTokens.has(parentPost.id) && !(parentPost.pfbid && acceptedPostTokens.has(parentPost.pfbid)))) continue;
    const container = link.closest('[role="article"]');
    if (!container || container === target || !visible(container)) continue;
    if (container.parentElement.closest('[role="article"]') !== target) continue;
    out.commentIds.push(cid);
    const texts = [...container.querySelectorAll('[dir="auto"]')].filter(el =>
      el.closest('[role="article"]') === container && !el.closest('[contenteditable="true"]'));
    const body = texts.find(el => norm(el.innerText || el.textContent) === norm(message));
    const authors = new Set();
    for (const author of ownLinks(container)) {
      // The author link precedes the comment body; arbitrary links in its text
      // cannot establish the identity of the author.
      const beforeBody = body && (author.compareDocumentPosition(body) & Node.DOCUMENT_POSITION_FOLLOWING) && !author.contains(body);
      if (!author.closest('h3,h4,[role="heading"]') && !beforeBody) continue;
      const id = profile(author.href); if (id) authors.add(id);
    }
    if (!authors.size && relayCommentAuthors.has(cid)) {
      const rAuthors = relayCommentAuthors.get(cid);
      if (rAuthors.size === 1) for (const id of rAuthors) authors.add(id);
    }
    if (body) {
      out.comments.push({id:cid, authorIds:[...authors], text:message});
    }
  }
  out.commentIds = [...new Set(out.commentIds)];
  return out;
})
""".replace('__ACCESS_STATE__', _BROWSER_ACCESS_STATE_JS)


def _numeric(value):
    value = str(value or "").strip()
    return value if re.fullmatch(r"[0-9]+", value) else None


def _pfbid(value):
    value = str(value or "").strip()
    return value if re.fullmatch(r"pfbid[A-Za-z0-9]{10,120}", value) else None


def _post_token(value):
    return _numeric(value) or _pfbid(value)


def _facebook_url(value):
    value = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname in
                 {"facebook.com", "www.facebook.com", "m.facebook.com"} and
                 not parsed.username and not parsed.password and parsed.port in (None, 443))
    except ValueError:
        valid = False
    if not valid or any(ord(ch) < 32 or ch == "\\" for ch in value):
        raise BrowserAutomationError("Chỉ hỗ trợ URL HTTPS của Facebook, không kèm tài khoản hoặc cổng lạ.")
    return value


def _post_identity(raw):
    raw = str(raw or "").strip()
    owner = None
    if "://" in raw:
        parsed = urllib.parse.urlsplit(_facebook_url(raw))
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("comment_id") or query.get("reply_comment_id"):
            raise BrowserAutomationError("Cần đường dẫn bài viết, không phải đường dẫn bình luận.")
        candidates = query.get("story_fbid") or []
        owner = _numeric((query.get("id") or [None])[0])
        match = re.fullmatch(r"/([^/]+)/posts/([0-9]+|pfbid[A-Za-z0-9]{10,120})/?", parsed.path)
        if match:
            candidates.append(match.group(2))
            owner = _numeric(match.group(1)) or owner
        video = re.fullmatch(r"/(?:[^/]+/)?(?:videos|reel)/([0-9]+)/?", parsed.path)
        if video:
            candidates.append(video.group(1))
        if len(set(candidates)) != 1 or not _post_token(candidates[0]):
            raise BrowserAutomationError("Cần ID bài viết dạng số hoặc pfbid hợp lệ của Facebook; liên kết rút gọn chưa hỗ trợ.")
        return owner, candidates[0]
    if re.fullmatch(r"[0-9]+_(?:[0-9]+|pfbid[A-Za-z0-9]{10,120})", raw):
        return tuple(raw.split("_", 1))
    if _post_token(raw):
        return None, raw
    raise BrowserAutomationError("ID bài viết không hợp lệ hoặc chưa có ID Facebook được hỗ trợ.")


def _page_context(page):
    profile_id = str(page.get("antidetect_profile_id") or "").strip()
    page_id = _numeric(page.get("page_id"))
    identity_id = _numeric(page.get("fb_profile_id")) or page_id
    if not profile_id or not page_id or not identity_id:
        raise BrowserAutomationError("Page cần ID dạng số và liên kết profile Antidetect hợp lệ.")
    try:
        profiles = [p for p in _read_profiles_readonly() if str(p.get("id") or "") == profile_id]
    except (OSError, ValueError, TypeError) as exc:
        raise BrowserAutomationError("Không đọc được danh mục profile Antidetect.", "NETWORK_RETRYABLE") from exc
    if len(profiles) != 1:
        raise BrowserAutomationError("Không tìm thấy đúng một profile trong danh mục Antidetect.")
    mapped = []
    for item in profiles[0].get("facebook_pages") or []:
        item_id = _numeric(item.get("id"))
        if not item_id and item.get("url"):
            parsed = urllib.parse.urlsplit(_facebook_url(item["url"]))
            item_id = _numeric(parsed.path.strip("/")) or _numeric(
                (urllib.parse.parse_qs(parsed.query).get("id") or [None])[0])
        if item_id == identity_id:
            mapped.append(item)
    if len(mapped) != 1:
        raise BrowserAutomationError("ID Page chưa được ánh xạ duy nhất vào profile Antidetect đã chọn.")
    if page.get("page_url"):
        _facebook_url(page["page_url"])
    return {"profile_id": profile_id, "page_id": page_id, "identity_id": identity_id,
            "allowed_ids": {page_id, identity_id}}


def _check_mapping(page, expected):
    current = _page_context(page)
    if any(current[key] != expected[key] for key in current):
        raise BrowserAutomationError("Ánh xạ Page/profile đã thay đổi; cần kiểm tra kết nối lại.")


@contextmanager
def _profile_operation(page, *, allow_blocked=False, kind="verify"):
    profile_id = str(page.get("antidetect_profile_id") or "").strip()
    if not profile_id:
        raise BrowserAutomationError("Page chưa liên kết profile Antidetect.")
    if not _numeric(page.get("page_id")):
        raise BrowserAutomationError("Page cần ID dạng số và liên kết profile Antidetect hợp lệ.")
    with _BRIDGE_LOCK:
        lock = _PROFILE_OPERATION_LOCKS.setdefault(profile_id, threading.Lock())
    if not lock.acquire(blocking=False):
        raise BrowserAutomationError("Profile đang xử lý một thao tác khác.", "BROWSER_BUSY", "READY")
    context = None
    reservation = None
    primary_error = None
    preserve_marker = False
    try:
        try:
            reservation = browser_safety.begin_operation(
                profile_id, page_id=str(page.get("page_id") or ""), kind=kind,
                allow_blocked=allow_blocked)
        except Exception:
            raise BrowserAutomationError("Không lưu được lượt thao tác an toàn; chưa mở trình duyệt.",
                                         "BROWSER_BLOCKED", "RECOVERY_REQUIRED") from None
        if not reservation["ready"]:
            status = reservation.get("browser_status") or "ERROR"
            reason = reservation.get("reason", "")
            category = ("BROWSER_LIMIT" if reason.startswith("WAIT_PROFILE_") else
                        "BROWSER_BUSY" if reason == "PROFILE_BUSY" else "BROWSER_BLOCKED")
            error = BrowserAutomationError(reservation["message"], category, status,
                                           retry_at=reservation.get("retry_at"))
            error.profile_safety_recorded = True
            error.safety_persistence_failed = bool(reservation.get("safety_persistence_failed"))
            raise error
        try:
            context = _page_context(page)
            context["safety_generation"] = reservation["generation"]
            context["operation_id"] = reservation["operation_id"]
            _check_mapping(page, context)
            yield context
        except BrowserAutomationError as exc:
            primary_error = exc
            preserve_marker = exc.safety_persistence_failed
            if exc.browser_status in ("LOGIN_REQUIRED", "CHECKPOINT", "RESTRICTED"):
                try:
                    recorded = browser_safety.block(profile_id, exc.browser_status, exc.message,
                                                     operation_id=reservation["operation_id"])
                    exc.safety_persistence_failed = bool(recorded.get("safety_persistence_failed"))
                    exc.profile_safety_recorded = not exc.safety_persistence_failed
                except Exception:
                    browser_safety.latch(profile_id, exc.browser_status, exc.message)
                    exc.safety_persistence_failed = True
                preserve_marker = exc.safety_persistence_failed
            raise
        except sqlite3.Error:
            # An unexpected safety/DB failure leaves the committed marker for
            # recovery. Never turn this operation into an ordinary clean retry.
            preserve_marker = True
            browser_safety.latch(profile_id, "RECOVERY_REQUIRED", "Thao tác bị gián đoạn; cần kiểm tra lại profile.")
            raise BrowserAutomationError("Không hoàn tất đối chiếu trạng thái an toàn; profile đã dừng.",
                                         "BROWSER_BLOCKED", "RECOVERY_REQUIRED") from None
    finally:
        try:
            if reservation and reservation.get("ready") and preserve_marker:
                browser_safety.abandon_operation(profile_id, reservation["operation_id"])
            if reservation and reservation.get("ready") and not preserve_marker:
                try:
                    finished = browser_safety.finish_operation(
                        profile_id, reservation["operation_id"], reservation["generation"])
                    failed = not finished.get("completed")
                    if failed:
                        browser_safety.latch(profile_id, "RECOVERY_REQUIRED", "Không xác nhận được kết thúc thao tác; cần kiểm tra profile.")
                except Exception:
                    browser_safety.latch(profile_id, "RECOVERY_REQUIRED", "Không lưu được kết thúc thao tác; cần kiểm tra lại profile.")
                    failed = True
                if failed:
                    if context is not None:
                        context["safety_persistence_failed"] = True
                    if primary_error is not None:
                        primary_error.safety_persistence_failed = True
                    elif kind != "send":
                        error = BrowserAutomationError("Không lưu được trạng thái kiểm tra; profile vẫn dừng.",
                                                        "BROWSER_BLOCKED", "RECOVERY_REQUIRED")
                        error.safety_persistence_failed = True
                        raise error
        finally:
            lock.release()


def build_facebook_post_url(page_id, post_id, page_url=None, post_url=None):
    canonical = _numeric(page_id)
    if not canonical:
        raise BrowserAutomationError("Page ID phải là dạng số.")
    owner, post_tok = _post_identity(post_id)
    if owner and owner != canonical:
        raise BrowserAutomationError("ID bài viết thuộc Page khác.")
    if page_url:
        _facebook_url(page_url)
    if post_url:
        supplied_owner, supplied_post = _post_identity(post_url)
        same_kind = bool(_numeric(supplied_post)) == bool(_numeric(post_tok))
        if (same_kind and supplied_post != post_tok) or (supplied_owner and supplied_owner != canonical):
            raise BrowserAutomationError("Đường dẫn không khớp ID bài viết/Page.")
        return _facebook_url(post_url)
    if "://" in str(post_id):
        return _facebook_url(post_id)
    if _pfbid(post_tok):
        return f"https://www.facebook.com/{canonical}/posts/{post_tok}"
    return f"https://www.facebook.com/permalink.php?story_fbid={post_tok}&id={canonical}"


def _target_post(context, post_id, post_url):
    owner, post_tok = _post_identity(post_id)
    if owner and owner not in context["allowed_ids"]:
        raise BrowserAutomationError("Bài viết không thuộc Page được cấu hình.")
    url = post_url or (str(post_id) if "://" in str(post_id) else None)
    required_tokens = {post_tok}
    if url:
        supplied_owner, supplied_post = _post_identity(url)
        same_kind = bool(_numeric(supplied_post)) == bool(_numeric(post_tok))
        if (same_kind and supplied_post != post_tok) or (supplied_owner and supplied_owner not in context["allowed_ids"]):
            raise BrowserAutomationError("Đường dẫn bài viết không khớp Page/ID đã chọn.")
        required_tokens.add(supplied_post)
        url = _facebook_url(url)
    elif _pfbid(post_tok):
        url = f"https://www.facebook.com/{context['identity_id']}/posts/{post_tok}"
    else:
        url = f"https://www.facebook.com/permalink.php?story_fbid={post_tok}&id={context['identity_id']}"
    return post_tok, url, required_tokens


def _timestamp(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", str(value)):
            stamp = float(value)
            if stamp > 10**11:
                stamp /= 1000
            result = datetime.fromtimestamp(stamp, timezone.utc)
        else:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None or result.year < 2004:
            return None
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _validate_state(state):
    if not isinstance(state, dict):
        raise BrowserAutomationError("Không đọc được trang Facebook qua CDP.", "NETWORK_RETRYABLE")
    access = state.get("accessState")
    if access == "login_required":
        raise BrowserAutomationError("Profile cần đăng nhập Facebook lại.", "TOKEN_EXPIRED", "LOGIN_REQUIRED")
    if access == "checkpoint":
        raise BrowserAutomationError("Profile đang ở bước xác minh Facebook.", "TOKEN_EXPIRED", "CHECKPOINT")
    if access == "restricted":
        raise BrowserAutomationError("Facebook đang hạn chế hoạt động của profile; cần xử lý và kiểm tra lại.",
                                     "ACTIVITY_RESTRICTED", "RESTRICTED")
    _facebook_url(state.get("url"))
    if access != "ok":
        raise BrowserAutomationError("Không xác định được trạng thái đăng nhập Facebook.")


def _identity_matches(state, context):
    ids = set(state.get("actorIds") or [])
    return bool(ids) and ids <= context["allowed_ids"] and all(_numeric(i) for i in ids)


def _verified_post(state, context, post_id, require_timestamp=False, *, required_tokens=None):
    _validate_state(state)
    route_owner, route_post = _post_identity(state["url"])
    if route_owner and route_owner not in context["allowed_ids"]:
        raise BrowserAutomationError("Facebook đã chuyển sang bài viết khác.")
    matches = [p for p in state.get("posts") or []
               if post_id in {str(p.get("id") or ""), str(p.get("pfbid") or "")}]
    if not matches:
        # Differentiate route redirect to an unrelated post vs missing article in DOM
        if route_post != post_id:
            raise BrowserAutomationError("Facebook đã chuyển sang bài viết khác.")
        raise BrowserAutomationError("Không xác minh được bài viết đang hiển thị.")
    valid = []
    must_match = set(required_tokens or {post_id})
    for item in matches:
        item_tokens = {t for t in (_post_token(item.get("id")), _post_token(item.get("pfbid"))) if t}
        if route_post not in item_tokens or not must_match <= item_tokens:
            raise BrowserAutomationError("Facebook đã chuyển sang bài viết khác.")
        owners = set(item.get("ownerIds") or [])
        if not owners or not owners <= context["allowed_ids"]:
            raise BrowserAutomationError("Không xác minh được ID chủ sở hữu bài viết đúng Page.")
        owner, observed_post = _post_identity(item.get("url"))
        if observed_post not in item_tokens or (owner and owner not in context["allowed_ids"]):
            raise BrowserAutomationError("Liên kết trong bài viết không khớp Page/ID.")
        valid.append(item)
    times = {_timestamp(p.get("created_time")) for p in valid} - {None}
    if len(times) > 1:
        raise BrowserAutomationError("Facebook trả về thời điểm bài viết không nhất quán.")
    created = next(iter(times), None)
    if require_timestamp and created is None:
        raise BrowserAutomationError("Chưa đọc được thời điểm đăng chính xác của bài viết.")
    canonical_tok = next((_numeric(p.get("id")) for p in valid if _numeric(p.get("id"))), None)
    if not canonical_tok:
        canonical_tok = _post_token(valid[0].get("id")) or post_id
    return {"clean_post_id": f"{context['page_id']}_{canonical_tok}",
            "permalink_url": _facebook_url(valid[0]["url"]),
            "created_time": created.isoformat() if created else None}


class _BrowserSession:
    def __init__(self, ws):
        self.ws = ws
        self.msg_id = 0

    async def call(self, method, params=None):
        self.msg_id += 1
        response = await _cdp_call(self.ws, self.msg_id, method, params)
        if response.get("error"):
            raise RuntimeError("CDP không hoàn tất thao tác.")
        return response

    async def inspect(self, post_id="", message=""):
        self.msg_id += 1
        expression = f"{_BROWSER_EVIDENCE_JS}({json.dumps(post_id)}, {json.dumps(message, ensure_ascii=False)})"
        state = await _cdp_eval(self.ws, self.msg_id, expression)
        _validate_state(state)
        return state

    async def wait_until_loaded(self):
        # A fixed sleep can inspect the blank shell before React or a slow
        # profile connection finishes loading, misreporting a logged-in Page.
        for attempt in range(20):
            self.msg_id += 1
            try:
                state = await _cdp_eval(self.ws, self.msg_id, _BROWSER_READINESS_JS)
            except RuntimeError as exc:
                # Navigation can replace the JS context after Page.navigate
                # returns. Only these specific protocol errors are transient.
                if not str(exc).startswith((
                    "CDP Runtime.evaluate failed: Execution context was destroyed",
                    "CDP Runtime.evaluate failed: Cannot find context with specified id",
                )):
                    raise
                state = None
            if isinstance(state, dict):
                if state.get("checkpointPresent"):
                    raise BrowserAutomationError("Profile đang ở bước xác minh Facebook.", "TOKEN_EXPIRED", "CHECKPOINT")
                if state.get("restrictedPresent"):
                    raise BrowserAutomationError("Facebook đang hạn chế hoạt động của profile; cần xử lý và kiểm tra lại.",
                                                 "ACTIVITY_RESTRICTED", "RESTRICTED")
                if state.get("loginPresent"):
                    raise BrowserAutomationError("Profile cần đăng nhập Facebook lại.", "TOKEN_EXPIRED", "LOGIN_REQUIRED")
                try:
                    _facebook_url(state.get("url"))
                except BrowserAutomationError:
                    # about:blank may still be visible before navigation commits.
                    if state.get("url") not in ("about:blank", ""):
                        raise
                else:
                    if state.get("readyState") == "complete" and state.get("mainPresent"):
                        return
            if attempt < 19:
                await asyncio.sleep(1)
        raise BrowserAutomationError(
            "Facebook chưa tải xong vùng nội dung sau 20 lần kiểm tra; chưa xác minh hoặc gửi bình luận.",
            "NETWORK_RETRYABLE")

    async def click(self, point):
        tx = float(point["x"]) + random.uniform(-1.5, 1.5)
        ty = float(point["y"]) + random.uniform(-1.5, 1.5)
        sx = getattr(self, "_cursor_x", random.uniform(18.0, 64.0))
        sy = getattr(self, "_cursor_y", random.uniform(18.0, 64.0))
        dist = math.hypot(tx - sx, ty - sy)
        steps = max(4, min(14, int(dist / 35) + 4))
        cx = (sx + tx) / 2.0 + random.uniform(-18.0, 18.0)
        cy = (sy + ty) / 2.0 + random.uniform(-18.0, 18.0)
        for i in range(1, steps + 1):
            t = i / steps
            t_ease = t * t * (3.0 - 2.0 * t)
            u = 1.0 - t_ease
            px = round(u * u * sx + 2.0 * u * t_ease * cx + t_ease * t_ease * tx, 1)
            py = round(u * u * sy + 2.0 * u * t_ease * cy + t_ease * t_ease * ty, 1)
            await self.call(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": px, "y": py, "button": "none", "buttons": 0, "pointerType": "mouse"},
            )
            await asyncio.sleep(random.uniform(0.008, 0.016))
        self._cursor_x, self._cursor_y = tx, ty
        await self.call(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": tx, "y": ty, "button": "left", "buttons": 1, "clickCount": 1, "pointerType": "mouse"},
        )
        await asyncio.sleep(random.uniform(0.065, 0.125))
        await self.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": tx, "y": ty, "button": "left", "buttons": 0, "clickCount": 1, "pointerType": "mouse"},
        )


async def _with_browser_tab(context, url, operation):
    import websockets
    port, opened_by_us, server_port = acquire_profile_cdp_port(context["profile_id"])
    target_id = ""
    try:
        target_id, ws_url = _open_transient_tab(port)
        async with websockets.connect(ws_url, open_timeout=5, max_size=10 * 1024 * 1024) as ws:
            session = _BrowserSession(ws)
            response = await session.call("Page.navigate", {"url": _facebook_url(url)})
            if (response.get("result") or {}).get("errorText"):
                raise BrowserAutomationError("Không mở được trang Facebook.", "NETWORK_RETRYABLE")
            await session.wait_until_loaded()
            return await operation(session)
    finally:
        if target_id:
            _close_transient_tab(port, target_id)
        release_profile_cdp_port(context["profile_id"], opened_by_us, server_port)


async def _confirm_actor(session, context, post_id="", message=""):
    state = await session.inspect(post_id, message)
    if _identity_matches(state, context):
        return state
    if state.get("actorButtonRect"):
        await session.click(state["actorButtonRect"])
        await asyncio.sleep(1)
        state = await session.inspect(post_id, message)
    options = [o for o in state.get("actorOptions") or [] if str(o.get("id")) in context["allowed_ids"]]
    if len(options) == 1:
        await session.click(options[0])
        await asyncio.sleep(2)
        state = await session.inspect(post_id, message)
    if not _identity_matches(state, context):
        raise BrowserAutomationError("Không xác nhận được ID Page đang bình luận. Tên Page giống nhau chưa đủ; cần mở đúng Page trong profile rồi kiểm tra lại.")
    return state


def _run_read(page, context, url, operation):
    try:
        result = asyncio.run(_with_browser_tab(context, url, operation))
        _check_mapping(page, context)
        return result
    except BrowserAutomationError:
        raise
    except Exception as exc:
        raise BrowserAutomationError(f"Không hoàn tất đọc Facebook qua CDP ({type(exc).__name__}).", "NETWORK_RETRYABLE") from exc


def verify_page_connection_cdp(page):
    try:
        with _profile_operation(page, allow_blocked=True, kind="check") as context:
            async def operation(session):
                state = await _confirm_actor(session, context)
                return {"success": True, "error": None, "browser_status": "READY",
                        "identity_id": context["identity_id"], "profile_id": context["profile_id"],
                        "observed_identity_ids": sorted(set(state["actorIds"]))}
            result = _run_read(page, context, f"https://www.facebook.com/{context['identity_id']}", operation)
            cleared = browser_safety.clear_block(context["profile_id"], context["safety_generation"],
                                                 operation_id=context["operation_id"])
            if not cleared["cleared"]:
                error = BrowserAutomationError("Trạng thái an toàn của profile đã đổi trong khi kiểm tra; chưa cho phép gửi.",
                                                "BROWSER_BLOCKED")
                error.safety_persistence_failed = bool(cleared.get("safety_persistence_failed"))
                raise error
            result["safety_generation"] = cleared["generation"]
        return result
    except BrowserAutomationError as exc:
        return {**exc.as_result(), "identity_id": None, "profile_id": page.get("antidetect_profile_id")}


def get_recent_posts_for_page_cdp(page, since_timestamp=None):
    with _profile_operation(page, kind="scan") as context:
        since = _timestamp(since_timestamp) if since_timestamp is not None else None
        if since_timestamp is not None and since is None:
            raise BrowserAutomationError("Mốc thời gian quét bài viết không hợp lệ.")
        async def operation(session):
            state = await _confirm_actor(session, context)
            results = {}
            timestamps = {}
            for item in state.get("posts") or []:
                post_id = _post_token(item.get("id"))
                created = _timestamp(item.get("created_time"))
                owners = set(item.get("ownerIds") or [])
                if not post_id or not created or not owners or not owners <= context["allowed_ids"]:
                    continue
                owner, url_post = _post_identity(item.get("url"))
                item_tokens = {t for t in (post_id, _post_token(item.get("pfbid"))) if t}
                if url_post not in item_tokens or (owner and owner not in context["allowed_ids"]):
                    continue
                if post_id in timestamps and timestamps[post_id] != created:
                    raise BrowserAutomationError("Facebook trả về thời điểm bài viết không nhất quán.")
                timestamps[post_id] = created
                if since and created <= since:
                    continue
                results[post_id] = {"id": f"{context['page_id']}_{post_id}", "created_time": created.isoformat(),
                                    "permalink_url": _facebook_url(item["url"])}
            return sorted(results.values(), key=lambda item: item["created_time"], reverse=True)
        return _run_read(page, context, f"https://www.facebook.com/{context['identity_id']}", operation)


def verify_post_for_page_cdp(page, post_id, post_url=None):
    with _profile_operation(page, kind="verify") as context:
        numeric_post, url, required_tokens = _target_post(context, post_id, post_url)
        async def operation(session):
            state = await _confirm_actor(session, context, numeric_post)
            return _verified_post(state, context, numeric_post, require_timestamp=True, required_tokens=required_tokens)
        return _run_read(page, context, url, operation)


def _normalize_comment(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _matching_comment_ids(state, context, message):
    matches = set()
    for item in state.get("comments") or []:
        authors = set(item.get("authorIds") or [])
        comment_id = _numeric(item.get("id"))
        if comment_id and authors and authors <= context["allowed_ids"] and \
                _normalize_comment(item.get("text")) == _normalize_comment(message):
            matches.add(comment_id)
    return matches


def send_page_comment_cdp(page, post_id, message, post_url=None, before_submit=None,
                          *, prepare_for_submit=None, final_submit_gate=None):
    submitted = False
    context = None
    ticket = submit_control.snapshot(str(page.get("page_id") or ""),
                                     profile_id=str(page.get("antidetect_profile_id") or ""))
    try:
        if not isinstance(message, str) or not message.strip():
            raise BrowserAutomationError("Nội dung bình luận không được để trống.")
        if _dispatch_paused():
            raise BrowserAutomationError("Hệ thống đang tạm dừng gửi bình luận.", "CANCELLED_BEFORE_SEND", "READY")
        with _profile_operation(page, kind="send") as context:
            numeric_post, url, required_tokens = _target_post(context, post_id, post_url)
            async def operation(session):
                nonlocal submitted
                state = await _confirm_actor(session, context, numeric_post, message)
                _verified_post(state, context, numeric_post, require_timestamp=True, required_tokens=required_tokens)
                _check_mapping(page, context)
                if not state.get("commentBoxRect") or state.get("commentBoxText"):
                    raise BrowserAutomationError("Không có ô bình luận trống được xác minh trong đúng bài viết.")
                if _matching_comment_ids(state, context, message):
                    raise BrowserAutomationError("Đã thấy bình luận trùng nội dung của Page; cần kiểm tra trước khi gửi lại.", "NEED_CHECK", "READY")
                baseline = set(state.get("commentIds") or [])
                await session.click(state["commentBoxRect"])
                await asyncio.sleep(random.uniform(0.18, 0.38))
                focused = await session.inspect(numeric_post, message)
                _verified_post(focused, context, numeric_post, required_tokens=required_tokens)
                if not focused.get("focused") or focused.get("commentBoxText") or not _identity_matches(focused, context):
                    raise BrowserAutomationError("Ô nhập hoặc Page đang bình luận đã thay đổi.")
                await session.call("Input.insertText", {"text": message})
                await asyncio.sleep(random.uniform(0.22, 0.45))
                final = await session.inspect(numeric_post, message)
                _verified_post(final, context, numeric_post, required_tokens=required_tokens)
                if not final.get("focused") or not _identity_matches(final, context) or \
                        _normalize_comment(final.get("commentBoxText")) != _normalize_comment(message):
                    raise BrowserAutomationError("Nội dung/ô nhập/Page thay đổi trước khi gửi; đã hủy thao tác.", "CANCELLED_BEFORE_SEND")
                baseline.update(final.get("commentIds") or [])
                _check_mapping(page, context)
                prepare = prepare_for_submit or before_submit
                if prepare is not None and not prepare():
                    raise BrowserAutomationError("Tác vụ đã dừng hoặc cấu hình thay đổi trước khi gửi.", "CANCELLED_BEFORE_SEND", "READY")
                # The callback may refresh Sheets over HTTP. DOM evidence from
                # before that potentially slow call is no longer sufficient.
                evidence_started = _submit_time()
                ready = await session.inspect(numeric_post, message)
                _verified_post(ready, context, numeric_post, required_tokens=required_tokens)
                if not ready.get("focused") or not _identity_matches(ready, context) or \
                        _normalize_comment(ready.get("commentBoxText")) != _normalize_comment(message):
                    raise BrowserAutomationError("Bài viết/Page/ô nhập thay đổi trong khi kiểm tra cấu hình; đã hủy gửi.", "CANCELLED_BEFORE_SEND")
                if _matching_comment_ids(ready, context, message):
                    raise BrowserAutomationError("Đã thấy bình luận trùng nội dung trước khi gửi; cần kiểm tra để tránh đăng lặp.", "NEED_CHECK", "READY")
                baseline.update(ready.get("commentIds") or [])
                _check_mapping(page, context)
                try:
                    with closing(sqlite3.connect(db.DB_PATH, timeout=0.2)) as safety_conn:
                        safety_conn.execute("PRAGMA query_only=ON")
                        safety = browser_safety.status(context["profile_id"], conn=safety_conn)
                except Exception:
                    browser_safety.latch(context["profile_id"], "RECOVERY_REQUIRED", "Không đọc được trạng thái an toàn trước gửi.")
                    error = BrowserAutomationError("Không đọc được trạng thái an toàn; chưa gửi bình luận.",
                                                   "BROWSER_BLOCKED", "RECOVERY_REQUIRED")
                    error.safety_persistence_failed = True
                    raise error from None
                if safety.get("safety_persistence_failed"):
                    blocked_status = safety.get("browser_status")
                    if blocked_status not in ("LOGIN_REQUIRED", "CHECKPOINT", "RESTRICTED"):
                        blocked_status = "RECOVERY_REQUIRED"
                    browser_safety.latch(context["profile_id"], blocked_status, "Không đối chiếu được trạng thái an toàn trước gửi.")
                    error = BrowserAutomationError("Không đối chiếu được trạng thái an toàn; chưa gửi bình luận.",
                                                   "BROWSER_BLOCKED", blocked_status)
                    error.safety_persistence_failed = True
                    raise error
                if safety.get("browser_status") != "READY" or safety.get("generation") != context["safety_generation"]:
                    raise BrowserAutomationError("Profile đã bị dừng trong khi chuẩn bị gửi.",
                                                 "BROWSER_BLOCKED", safety.get("browser_status") or "ERROR")
                if _submit_time() - evidence_started > _MAX_SUBMIT_EVIDENCE_AGE_SEC:
                    raise BrowserAutomationError("Bằng chứng bài viết đã cũ; đã hủy trước khi gửi.", "CANCELLED_BEFORE_SEND", "READY")
                if final_submit_gate is not None and not final_submit_gate():
                    raise BrowserAutomationError("Quyền gửi hoặc cấu hình đã thay đổi; đã hủy trước khi gửi.", "CANCELLED_BEFORE_SEND", "READY")
                if (_submit_time() - evidence_started > _MAX_SUBMIT_EVIDENCE_AGE_SEC
                        or not submit_control.commit(ticket, _dispatch_paused)):
                    raise BrowserAutomationError("Lượt gửi đã bị dừng hoặc quá hạn trước khi chốt.", "CANCELLED_BEFORE_SEND", "READY")
                # Set before dispatch: a lost response to keyDown is uncertain,
                # even if the subsequent keyUp never completes.
                submitted = True
                await session.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter",
                    "text": "\r", "unmodifiedText": "\r", "windowsVirtualKeyCode": 13})
                await session.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter",
                    "windowsVirtualKeyCode": 13})
                for _ in range(8):
                    await asyncio.sleep(1)
                    after = await session.inspect(numeric_post, message)
                    _verified_post(after, context, numeric_post, required_tokens=required_tokens)
                    fresh = _matching_comment_ids(after, context, message) - baseline
                    if len(fresh) == 1:
                        return {"success": True, "category": "SUCCESS", "comment_id": fresh.pop(),
                                "error": None, "browser_status": "READY", "mode": "ANTIDETECT_CDP", "usage_pct": None}
                raise BrowserAutomationError("Đã gửi Enter nhưng chưa xác nhận được ID bình luận mới đúng Page/nội dung. Cần kiểm tra, không tự gửi lại.", "NEED_CHECK", "READY")
            result = asyncio.run(_with_browser_tab(context, url, operation))
        if context.get("safety_persistence_failed"):
            result["safety_persistence_failed"] = True
        return result
    except BrowserAutomationError as exc:
        if submitted:
            uncertain = BrowserAutomationError("Kết quả gửi chưa xác định. Cần kiểm tra bài viết trước khi gửi lại.",
                                               "NEED_CHECK", exc.browser_status)
            uncertain.profile_safety_recorded = exc.profile_safety_recorded
            uncertain.safety_persistence_failed = exc.safety_persistence_failed
            return uncertain.as_result()
        return exc.as_result()
    except Exception as exc:
        failed_safety = bool(context and context.get("safety_persistence_failed"))
        error = BrowserAutomationError(
            f"Lỗi CDP ({type(exc).__name__}); " + ("cần kiểm tra bài viết trước khi gửi lại." if submitted else "chưa gửi bình luận."),
            "NEED_CHECK" if submitted else ("BROWSER_BLOCKED" if failed_safety else "NETWORK_RETRYABLE"),
            "RECOVERY_REQUIRED" if failed_safety else "ERROR")
        error.safety_persistence_failed = failed_safety
        return error.as_result()


# Compatibility entry points delegate to the same numeric-identity safety gates.
def get_recent_posts_cdp(page_id, page_url, profile_id, since_timestamp=None, limit=5):
    page = {"page_id": page_id, "page_url": page_url, "antidetect_profile_id": profile_id}
    return get_recent_posts_for_page_cdp(page, since_timestamp)[:limit]


def post_comment_cdp(post_id, page_id, page_name, page_url, profile_id, message, post_url=None):
    page = {"page_id": page_id, "page_name": page_name, "page_url": page_url, "antidetect_profile_id": profile_id}
    return send_page_comment_cdp(page, post_id, message, post_url=post_url)


def _normalize_actor_name(name):
    text = str(name or "").translate({ord(ch): None for ch in "\u200b\u200c\u200d\ufeff"})
    return re.sub(r"\s+", " ", text.strip().lower())


def _actor_names_match(actor_names, page_name):
    """Display helper only; names never authorize sending."""
    target = _normalize_actor_name(page_name)
    names = [_normalize_actor_name(n) for n in actor_names or [] if isinstance(n, str) and n.strip()]
    return bool(target and names) and all(n == target for n in names)


def _select_exact_actor_option(candidates, page_name):
    """Legacy display helper only; production switching uses numeric IDs."""
    target = _normalize_actor_name(page_name)
    picked, seen = [], set()
    for candidate in candidates if isinstance(candidates, list) else []:
        if not isinstance(candidate, dict) or not target or _normalize_actor_name(candidate.get("text")) != target:
            continue
        try:
            key = (int(float(candidate.get("x") or 0)) // 10, int(float(candidate.get("y") or 0)) // 10)
        except (ValueError, TypeError):
            continue
        if key not in seen:
            seen.add(key)
            picked.append(candidate)
    return picked
