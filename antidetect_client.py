"""
Bridge & CDP Automation Client for C:\\Projects\\Tools\\Antidetect Python.

Safety & Integrity Guarantees (per Antidetect Python HANDOFF.md):
1. NEVER writes to or modifies `data/profiles.json`, `secrets.dpapi`, or any profile
   metadata file inside `Antidetect Python`. All profile/fanpage discovery is 100% read-only.
2. Respects browser ownership: if a profile is already open in Antidetect Python,
   this module only opens a transient CDP tab (`/json/new?about%3Ablank`), performs
   the scan/comment, and closes the transient tab — NEVER closing the user's browser.
3. Uses CDP Input domain (`Input.dispatchMouseEvent`, `Input.dispatchKeyEvent`,
   `Input.insertText`) with Bezier mouse trajectories and lognormal keystroke delays
   from `antidetect.human.HumanInput` — zero JS injection on the page.
"""
import asyncio
import csv
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
import database as db
import meta_client

_BRIDGE_LOCK = threading.RLock()
_INPROC_MANAGER = None
_INPROC_LAUNCHER = None


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
    Returns `(server_port, access_key)` if available and healthy, else `None`.
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
                return None
            health = json.loads(resp.read().decode("utf-8"))
            if health.get("application") != "antidetect-python":
                return None

        if _ensure_antidetect_sys_path():
            from antidetect.runtime_auth import read_runtime
            rec = read_runtime() or {}
            access_key = rec.get("access_key")
            if access_key:
                return port, str(access_key)
    except Exception:
        return None
    return None


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


def acquire_profile_cdp_port(profile_id: str) -> Tuple[int, bool, Optional[int]]:
    """
    Ensures the specified Antidetect profile is running and returns:
      `(cdp_port, opened_by_us, server_port_if_started_via_server)`
    If the profile was already running before this call, `opened_by_us` is False
    so the caller will NOT stop the browser after finishing.
    """
    with _BRIDGE_LOCK:
        # 1. Check if already running in browser_runtime.json
        rt = _read_runtime_readonly().get(profile_id) or {}
        existing_port = rt.get("port")
        if _probe_cdp_port(existing_port):
            return int(existing_port), False, None

        # 2. Check if already running in our in-process launcher
        if _INPROC_LAUNCHER is not None:
            inproc_port = getattr(_INPROC_LAUNCHER, "active_ports", {}).get(profile_id)
            if _probe_cdp_port(inproc_port):
                return int(inproc_port), False, None

        # 3. Try starting via running Antidetect FastAPI server first
        srv = _get_running_antidetect_server()
        if srv is not None:
            srv_port, access_key = srv
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{srv_port}/api/profiles/{profile_id}/start",
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
                    already = bool(res.get("already_running"))
                    if _probe_cdp_port(port):
                        return int(port), (not already), srv_port
            except Exception:
                pass

        # 4. Launch in-process via BrowserLauncher
        _, launcher = _get_inproc_launcher()
        if launcher.is_running(profile_id):
            port = launcher.active_ports.get(profile_id)
            if _probe_cdp_port(port):
                return int(port), False, None

        res = launcher.launch(profile_id)
        if not res or not res.get("success"):
            err = (res or {}).get("error") or "Không thể khởi động profile Antidetect."
            raise RuntimeError(err)
        port = res.get("port") or launcher.active_ports.get(profile_id)
        if not _probe_cdp_port(port):
            raise RuntimeError(f"Profile {profile_id} đã mở nhưng cổng CDP ({port}) không phản hồi.")
        return int(port), True, None


def release_profile_cdp_port(profile_id: str, opened_by_us: bool, srv_port: Optional[int] = None) -> None:
    """
    Stops the profile ONLY if `opened_by_us` is True and `config.ANTIDETECT_AUTO_CLOSE_BROWSER` is True.
    Never closes a profile that the user had opened manually!
    """
    if not opened_by_us or not config.ANTIDETECT_AUTO_CLOSE_BROWSER:
        return
    with _BRIDGE_LOCK:
        if srv_port is not None:
            srv = _get_running_antidetect_server()
            if srv is not None:
                _, access_key = srv
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{srv_port}/api/profiles/{profile_id}/stop",
                        data=b"{}",
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {access_key}",
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=10.0):
                        return
                except Exception:
                    pass
        if _INPROC_LAUNCHER is not None:
            try:
                _INPROC_LAUNCHER.stop(profile_id)
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


def _open_transient_tab(port: int) -> Tuple[str, str]:
    """Opens a temporary blank tab on the CDP port and returns `(target_id, ws_url)`."""
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
    return target_id, str(ws_url)


def _close_transient_tab(port: int, target_id: str) -> None:
    if not target_id:
        return
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


# ============================================================================
# 1. SCAN RECENT POSTS OF A FANPAGE VIA CDP
# ============================================================================

_SCAN_POSTS_JS = r"""
(() => {
    const out = {
        url: location.href,
        accessState: 'ok',
        posts: []
    };
    const pathLower = String(location.pathname || '').toLowerCase();
    if (pathLower.startsWith('/checkpoint')) {
        out.accessState = 'checkpoint';
        return out;
    }
    if (pathLower.startsWith('/login') || (document.querySelector('input[name="email"]') && document.querySelector('input[name="pass"]'))) {
        out.accessState = 'login_required';
        return out;
    }

    const PINNED = /(?:^|\n)\s*(?:bài viết đã ghim|đã ghim|pinned post|pinned)\b/i;
    const PROMOTED = /(?:^|\n)\s*(?:được tài trợ|sponsored)\b/i;
    const RXS = [
        [/^(?:vừa xong|just now|now)$/i, 'now'],
        [/^(\d+)\s*(?:phút|phut|p|m|min|mins|minute|minutes)(?:\s*trước|\s*ago)?$/i, 'm'],
        [/^(\d+)\s*(?:giờ|gio|g|h|hr|hrs|hour|hours)(?:\s*trước|\s*ago)?$/i, 'h'],
        [/^(\d+)\s*(?:ngày|ngay|d|day|days)(?:\s*trước|\s*ago)?$/i, 'd']
    ];

    const articles = Array.from(document.querySelectorAll('div[role="article"]'));
    const seenUrls = new Set();

    for (const art of articles) {
        if (out.posts.length >= 10) break;
        if (art.parentElement && art.parentElement.closest('div[role="article"]')) continue;
        const headText = (art.innerText || '').trim().slice(0, 260);
        if (PINNED.test(headText) || PROMOTED.test(headText)) continue;

        const links = Array.from(art.querySelectorAll('a[href]'));
        let bestLink = null;
        let ageUnit = null;
        let ageVal = null;
        let rawLabel = null;

        for (const a of links) {
            const href = a.href || '';
            if (!href || href.includes('comment_id=') || href.includes('reply_comment_id=')) continue;
            const label = (a.getAttribute('aria-label') || a.textContent || '').trim().replace(/\s+/g, ' ');
            const isPostUrl = /\/posts\/|\/permalink\.php|\/videos\/|\/reel\/|\/photos\/|story_fbid=|fbid=/.test(href);

            if (label && label.length <= 45) {
                for (const [rx, u] of RXS) {
                    const m = label.match(rx);
                    if (m) {
                        ageVal = (u === 'now') ? 0 : parseInt(m[1], 10);
                        ageUnit = u;
                        rawLabel = label;
                        if (isPostUrl) bestLink = href.split('#')[0];
                        break;
                    }
                }
            }
            if (!bestLink && isPostUrl) {
                bestLink = href.split('#')[0];
            }
        }

        if (bestLink && ageUnit !== null && !seenUrls.has(bestLink)) {
            seenUrls.add(bestLink);
            out.posts.push({
                url: bestLink,
                ageVal: ageVal,
                ageUnit: ageUnit,
                rawLabel: rawLabel
            });
        }
    }
    return out;
})()
"""


def _extract_post_id_from_permalink(url: str, page_id: str) -> Optional[str]:
    clean = (url or "").strip()
    if not clean:
        return None
    sub_id = meta_client.extract_post_id_from_input(clean, page_id)
    if sub_id:
        return sub_id
    return None


def get_recent_posts_cdp(
    page_id: str,
    page_url: Optional[str],
    profile_id: str,
    since_timestamp: Optional[str] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """
    Opens a transient CDP tab on `profile_id`, navigates to the Fanpage URL,
    and extracts recent posts published on the Page.
    """
    target_url = (page_url or "").strip() or f"https://www.facebook.com/{page_id}"

    async def _run() -> List[Dict[str, Any]]:
        import websockets
        port, opened_by_us, srv_port = acquire_profile_cdp_port(profile_id)
        target_id = ""
        try:
            target_id, ws_url = _open_transient_tab(port)
            async with websockets.connect(ws_url, open_timeout=5, max_size=10 * 1024 * 1024) as ws:
                msg_id = 100
                msg_id += 1
                await _cdp_call(ws, msg_id, "Page.navigate", {"url": target_url})
                await asyncio.sleep(3.5)

                # Scroll slightly to trigger feed rendering
                msg_id += 1
                await _cdp_eval(ws, msg_id, "window.scrollBy(0, 500); true;")
                await asyncio.sleep(1.5)

                msg_id += 1
                data = await _cdp_eval(ws, msg_id, _SCAN_POSTS_JS)
                if not isinstance(data, dict):
                    return []

                access = data.get("accessState")
                if access in ("login_required", "checkpoint"):
                    raise meta_client.MetaAPIError(
                        f"Profile Antidetect bị mất đăng nhập hoặc checkpoint ({access}).",
                        code=190,
                        category="TOKEN_EXPIRED",
                    )

                now_utc = datetime.now(timezone.utc)
                results = []
                for item in (data.get("posts") or [])[:limit]:
                    p_url = item.get("url")
                    u = item.get("ageUnit")
                    val = int(item.get("ageVal") or 0)
                    if u == "now":
                        created_dt = now_utc
                    elif u == "m":
                        created_dt = now_utc - timedelta(minutes=val)
                    elif u == "h":
                        created_dt = now_utc - timedelta(hours=val)
                    elif u == "d":
                        created_dt = now_utc - timedelta(days=val)
                    else:
                        continue

                    post_id = _extract_post_id_from_permalink(p_url, page_id)
                    if not post_id:
                        continue
                    results.append({
                        "id": post_id,
                        "created_time": created_dt.isoformat(),
                        "permalink_url": p_url,
                    })
                return results
        finally:
            if target_id:
                _close_transient_tab(port, target_id)
            release_profile_cdp_port(profile_id, opened_by_us, srv_port)

    return asyncio.run(_run())


# ============================================================================
# 2. POST COMMENT AS FANPAGE VIA CDP + HUMAN INPUT
# ============================================================================

def build_facebook_post_url(page_id: str, post_id: str, page_url: Optional[str] = None, post_url: Optional[str] = None) -> str:
    """Constructs a navigable Facebook URL for a post_id."""
    if post_url and post_url.startswith("http"):
        return post_url
    raw = (post_id or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    sub_id = raw
    if "_" in raw:
        parts = raw.split("_", 1)
        if parts[0].isdigit():
            page_id = parts[0]
            sub_id = parts[1]
    if sub_id.startswith("pfbid"):
        return f"https://www.facebook.com/{page_id}/posts/{sub_id}"
    return f"https://www.facebook.com/permalink.php?story_fbid={sub_id}&id={page_id}"


_INSPECT_POST_AND_ACTOR_JS = r"""
((expectedPageName) => {
    const out = {
        url: location.href,
        accessState: 'ok',
        actingAsPage: false,
        actorButtonRect: null,
        switchProfileButtonRect: null,
        commentBoxRect: null,
        commentBoxPlaceholder: ''
    };
    const pathLower = String(location.pathname || '').toLowerCase();
    if (pathLower.startsWith('/checkpoint')) {
        out.accessState = 'checkpoint';
        return out;
    }
    if (pathLower.startsWith('/login') || (document.querySelector('input[name="email"]') && document.querySelector('input[name="pass"]'))) {
        out.accessState = 'login_required';
        return out;
    }

    const norm = (s) => String(s || '').toLowerCase().trim().replace(/\s+/g, ' ');
    const targetName = norm(expectedPageName);

    const rectOf = (el) => {
        if (!el) return null;
        el.scrollIntoView({block: 'center', inline: 'center'});
        const r = el.getBoundingClientRect();
        if (r.width <= 2 || r.height <= 2) return null;
        return {
            x: Math.round(r.left + r.width / 2),
            y: Math.round(r.top + r.height / 2),
            w: Math.round(r.width),
            h: Math.round(r.height)
        };
    };

    // 1. Check if page header has "Đang tương tác dưới tên..." or "Chuyển ngay" / "Switch Now"
    const bodyText = norm(document.body ? document.body.innerText : '');
    if (targetName && (
        bodyText.includes('đang tương tác dưới tên ' + targetName) ||
        bodyText.includes('interacting as ' + targetName) ||
        bodyText.includes('bình luận dưới tên ' + targetName) ||
        bodyText.includes('comment as ' + targetName)
    )) {
        out.actingAsPage = true;
    }

    // 2. Find comment textbox (contenteditable="true" and role="textbox")
    const boxes = Array.from(document.querySelectorAll('div[contenteditable="true"][role="textbox"]'));
    let targetBox = null;
    for (const b of boxes) {
        const label = norm(b.getAttribute('aria-label') || b.getAttribute(' aria-placeholder') || '');
        if (label.includes('bình luận') || label.includes('comment') || label.includes('trả lời') || label.includes('reply')) {
            targetBox = b;
            out.commentBoxPlaceholder = label;
            if (targetName && label.includes(targetName)) {
                out.actingAsPage = true;
            }
            break;
        }
    }
    if (!targetBox && boxes.length > 0) {
        targetBox = boxes[0];
        out.commentBoxPlaceholder = norm(targetBox.getAttribute('aria-label') || '');
    }
    if (targetBox) {
        out.commentBoxRect = rectOf(targetBox);
    }

    // 3. Look for inline Actor Switcher button near the comment area
    const buttons = Array.from(document.querySelectorAll('[role="button"], button'));
    for (const btn of buttons) {
        const aria = norm(btn.getAttribute('aria-label') || btn.textContent || '');
        if (
            aria.includes('tư cách') ||
            aria.includes('đang tương tác') ||
            aria.includes('bình luận dưới tên') ||
            aria.includes('comment as') ||
            aria.includes('interacting as') ||
            aria.includes('chọn người bình luận')
        ) {
            if (targetName && aria.includes(targetName)) {
                out.actingAsPage = true;
            }
            const rc = rectOf(btn);
            if (rc) {
                out.actorButtonRect = rc;
                break;
            }
        }
    }

    // 4. Look for Page-level "Chuyển ngay" / "Switch Now" / "Chuyển" button if not acting as page
    if (!out.actingAsPage) {
        for (const btn of buttons) {
            const txt = norm(btn.getAttribute('aria-label') || btn.textContent || '');
            if (txt === 'chuyển ngay' || txt === 'switch now' || txt === 'chuyển sang trang' || txt.includes('chuyển sang ' + targetName)) {
                const rc = rectOf(btn);
                if (rc) {
                    out.switchProfileButtonRect = rc;
                    break;
                }
            }
        }
    }

    return out;
})
"""

_FIND_ACTOR_MENU_OPTION_JS = r"""
((expectedPageName) => {
    const norm = (s) => String(s || '').toLowerCase().trim().replace(/\s+/g, ' ');
    const targetName = norm(expectedPageName);
    if (!targetName) return null;

    const candidates = Array.from(document.querySelectorAll('[role="menuitem"], [role="menuitemradio"], [role="option"], [role="dialog"] [role="button"], [role="menu"] [role="button"]'));
    for (const el of candidates) {
        const txt = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
        if (txt.includes(targetName)) {
            el.scrollIntoView({block: 'center', inline: 'center'});
            const r = el.getBoundingClientRect();
            if (r.width > 4 && r.height > 4) {
                return {
                    x: Math.round(r.left + r.width / 2),
                    y: Math.round(r.top + r.height / 2)
                };
            }
        }
    }
    return null;
})
"""

_VERIFY_COMMENT_APPEARED_JS = r"""
((commentSnippet) => {
    const norm = (s) => String(s || '').toLowerCase().trim().replace(/\s+/g, ' ');
    const needle = norm(commentSnippet).slice(0, 40);
    if (!needle) return {found: false};

    // Check outside of contenteditable input boxes
    const articles = Array.from(document.querySelectorAll('div[role="article"], ul li, div[dir="auto"]'));
    for (const el of articles) {
        if (el.isContentEditable || el.closest('[contenteditable="true"]')) continue;
        const txt = norm(el.innerText || el.textContent || '');
        if (txt.includes(needle)) {
            return {found: true};
        }
    }
    return {found: false};
})
"""


async def _human_click_on_ws(ws, msg_id: int, x: float, y: float, human_helper=None) -> int:
    """Dispatches human-like Bezier mouse movement and press/release click on the given CDP WebSocket."""
    if human_helper is not None:
        fx, fy = human_helper.rng.uniform(40, 180), human_helper.rng.uniform(40, 180)
        path = human_helper.mouse_path(fx, fy, x, y)
    else:
        path = [(x, y)]

    for px, py in path:
        msg_id += 1
        await _cdp_call(ws, msg_id, "Input.dispatchMouseEvent", {
            "type": "mouseMoved",
            "x": float(px),
            "y": float(py),
            "button": "none",
            "pointerType": "mouse",
        })
        await asyncio.sleep(0.01)

    msg_id += 1
    await _cdp_call(ws, msg_id, "Input.dispatchMouseEvent", {
        "type": "mousePressed",
        "x": float(x),
        "y": float(y),
        "button": "left",
        "buttons": 1,
        "clickCount": 1,
        "pointerType": "mouse",
    })
    await asyncio.sleep(0.08)
    msg_id += 1
    await _cdp_call(ws, msg_id, "Input.dispatchMouseEvent", {
        "type": "mouseReleased",
        "x": float(x),
        "y": float(y),
        "button": "left",
        "buttons": 0,
        "clickCount": 1,
        "pointerType": "mouse",
    })
    return msg_id


async def _human_type_and_submit_on_ws(ws, msg_id: int, text: str, human_helper=None) -> int:
    """
    Types Vietnamese/Unicode text with human flight delays via CDP Input domain
    and presses Enter to submit the comment.
    """
    await asyncio.sleep(0.25)
    # Type in natural small chunks/characters so React/Lexical editor state updates properly
    for ch in text:
        msg_id += 1
        await _cdp_call(ws, msg_id, "Input.insertText", {"text": ch})
        delay_s = (human_helper.key_delay_ms(ch) / 1000.0) if human_helper else 0.045
        await asyncio.sleep(min(0.18, max(0.025, delay_s * 0.5)))

    await asyncio.sleep(0.35)
    # Press Enter to submit comment
    msg_id += 1
    await _cdp_call(ws, msg_id, "Input.dispatchKeyEvent", {
        "type": "keyDown",
        "key": "Enter",
        "code": "Enter",
        "text": "\r",
        "unmodifiedText": "\r",
        "windowsVirtualKeyCode": 13,
        "nativeVirtualKeyCode": 13,
    })
    await asyncio.sleep(0.07)
    msg_id += 1
    await _cdp_call(ws, msg_id, "Input.dispatchKeyEvent", {
        "type": "keyUp",
        "key": "Enter",
        "code": "Enter",
        "windowsVirtualKeyCode": 13,
        "nativeVirtualKeyCode": 13,
    })
    return msg_id


def post_comment_cdp(
    post_id: str,
    page_id: str,
    page_name: str,
    page_url: Optional[str],
    profile_id: str,
    message: str,
    post_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Posts a comment under the Fanpage's identity using the owning Antidetect profile's CDP port.
    Returns a result dictionary compatible with `meta_client.post_comment`:
      `{"success": True/False, "comment_id": ..., "category": ..., "error": ...}`
    """
    target_post_url = build_facebook_post_url(page_id, post_id, page_url=page_url, post_url=post_url)

    async def _run() -> Dict[str, Any]:
        import websockets
        try:
            port, opened_by_us, srv_port = acquire_profile_cdp_port(profile_id)
        except Exception as e:
            return {
                "success": False,
                "category": "NETWORK_RETRYABLE",
                "error": f"Không mở được profile Antidetect ({profile_id}): {e}",
                "usage_pct": None,
            }

        human_helper = None
        if _ensure_antidetect_sys_path():
            try:
                from antidetect.human import HumanInput
                human_helper = HumanInput(port=port)
            except Exception:
                human_helper = None

        target_id = ""
        try:
            target_id, ws_url = _open_transient_tab(port)
            async with websockets.connect(ws_url, open_timeout=5, max_size=10 * 1024 * 1024) as ws:
                msg_id = 1
                msg_id += 1
                await _cdp_call(ws, msg_id, "Page.bringToFront")
                msg_id += 1
                await _cdp_call(ws, msg_id, "Page.navigate", {"url": target_post_url})
                await asyncio.sleep(4.0)

                # Inspect post, login state, actor switcher, and comment box
                escaped_name = json.dumps(page_name or "", ensure_ascii=False)
                msg_id += 1
                state = await _cdp_eval(ws, msg_id, f"{_INSPECT_POST_AND_ACTOR_JS}({escaped_name})")
                if not isinstance(state, dict):
                    return {
                        "success": False,
                        "category": "NETWORK_RETRYABLE",
                        "error": "Không đọc được trạng thái trang bài viết qua CDP.",
                        "usage_pct": None,
                    }

                access = state.get("accessState")
                if access in ("login_required", "checkpoint"):
                    return {
                        "success": False,
                        "category": "TOKEN_EXPIRED",
                        "error": f"Profile Antidetect bị mất phiên đăng nhập hoặc dính checkpoint ({access}).",
                        "usage_pct": None,
                    }

                # Try switching actor to Fanpage if not already acting as the Fanpage
                if not state.get("actingAsPage"):
                    if state.get("actorButtonRect"):
                        rc = state["actorButtonRect"]
                        msg_id = await _human_click_on_ws(ws, msg_id, rc["x"], rc["y"], human_helper)
                        await asyncio.sleep(1.2)
                        msg_id += 1
                        opt_rc = await _cdp_eval(ws, msg_id, f"{_FIND_ACTOR_MENU_OPTION_JS}({escaped_name})")
                        if isinstance(opt_rc, dict) and opt_rc.get("x") and opt_rc.get("y"):
                            msg_id = await _human_click_on_ws(ws, msg_id, opt_rc["x"], opt_rc["y"], human_helper)
                            await asyncio.sleep(1.8)
                    elif state.get("switchProfileButtonRect"):
                        rc = state["switchProfileButtonRect"]
                        msg_id = await _human_click_on_ws(ws, msg_id, rc["x"], rc["y"], human_helper)
                        await asyncio.sleep(3.5)

                    # Re-inspect comment box after actor switch
                    msg_id += 1
                    state = await _cdp_eval(ws, msg_id, f"{_INSPECT_POST_AND_ACTOR_JS}({escaped_name})") or state

                # Hard safety gate: never type a comment unless the page identity is confirmed.
                if not state.get("actingAsPage"):
                    return {
                        "success": False,
                        "category": "INVALID_TARGET",
                        "error": (
                            f"Không xác nhận được đang bình luận dưới tên Page '{page_name}'. "
                            "Đã chặn gửi để tránh bình luận bằng tài khoản cá nhân hoặc Page khác."
                        ),
                        "usage_pct": None,
                    }

                box_rect = state.get("commentBoxRect")
                if not box_rect:
                    # Scroll down a bit and look for the comment box again
                    msg_id += 1
                    await _cdp_eval(ws, msg_id, "window.scrollBy(0, 350); true;")
                    await asyncio.sleep(1.0)
                    msg_id += 1
                    state = await _cdp_eval(ws, msg_id, f"{_INSPECT_POST_AND_ACTOR_JS}({escaped_name})") or state
                    box_rect = state.get("commentBoxRect")

                if not box_rect:
                    return {
                        "success": False,
                        "category": "INVALID_TARGET",
                        "error": f"Không tìm thấy ô nhập bình luận trên bài viết ({target_post_url}).",
                        "usage_pct": None,
                    }

                # Click the comment input box, type the message, and press Enter
                msg_id = await _human_click_on_ws(ws, msg_id, box_rect["x"], box_rect["y"], human_helper)
                msg_id = await _human_type_and_submit_on_ws(ws, msg_id, message, human_helper)

                # Poll up to 6 seconds to verify the comment rendered in the post DOM
                escaped_msg = json.dumps(message, ensure_ascii=False)
                for _ in range(6):
                    await asyncio.sleep(1.0)
                    msg_id += 1
                    ver = await _cdp_eval(ws, msg_id, f"{_VERIFY_COMMENT_APPEARED_JS}({escaped_msg})")
                    if isinstance(ver, dict) and ver.get("found"):
                        return {
                            "success": True,
                            "comment_id": f"cdp_{page_id}_{int(time.time())}",
                            "mode": "ANTIDETECT_CDP",
                            "usage_pct": None,
                        }

                # Uncertain outcome after sending Enter -> mark NEED_CHECK to prevent duplicate comments
                return {
                    "success": False,
                    "category": "NEED_CHECK",
                    "error": "Đã gõ và nhấn Enter gửi bình luận qua Antidetect CDP nhưng chưa xác nhận được bình luận hiển thị trên DOM. Vui lòng kiểm tra bài viết trước khi thử lại.",
                    "usage_pct": None,
                }
        except Exception as e:
            return {
                "success": False,
                "category": "NEED_CHECK",
                "error": f"Lỗi kết nối CDP trong lúc gửi bình luận ({type(e).__name__}: {e}). Cần kiểm tra trên Facebook trước khi gửi lại.",
                "usage_pct": None,
            }
        finally:
            if target_id:
                _close_transient_tab(port, target_id)
            release_profile_cdp_port(profile_id, opened_by_us, srv_port)

    return asyncio.run(_run())
