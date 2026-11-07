import re
import csv
import io
import json
import httpx
from typing import Dict, Any, List, Optional, Tuple

import database as db

PAGE_ID_COLS = {"page id", "page_id", "id page", "id"}
PAGE_NAME_COLS = {"tên gợi nhớ", "page name", "page_name", "tên page", "ten page", "tên fanpage", "fanpage"}
COMMENT_TPL_COLS = {
    "mẫu bình luận", "mau binh luan", "comment", "template",
    "comment_template", "nội dung comment", "noi dung comment", "nội dung bình luận", "binh luan"
}
DELAY_COLS = {
    "delay (phút)", "delay", "thời gian chờ", "delay_minutes",
    "sau bao lâu comment (phút)", "sau bao lâu comment", "so phut cho", "số phút chờ"
}
LIMIT_COLS = {
    "giới hạn comment/ngày", "giới hạn", "max_comments_per_day",
    "mỗi ngày mấy comment", "moi ngay may comment", "số comment/ngày", "so comment/ngay"
}
ENABLED_COLS = {"bật tự động", "bật/tắt", "enabled", "bật", "is_enabled", "trạng thái"}

PAGE_CONFIG_COLS = DELAY_COLS | LIMIT_COLS | ENABLED_COLS | PAGE_NAME_COLS


def extract_sheet_id(url_or_id: str) -> Optional[str]:
    url_or_id = url_or_id.strip()
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', url_or_id)
    if match:
        return match.group(1)
    if re.match(r'^[a-zA-Z0-9-_]{20,}$', url_or_id):
        return url_or_id
    return None


def extract_sheet_gid(url_or_id: str) -> Optional[str]:
    m = re.search(r'[?#&]gid=([0-9]+)', url_or_id or "")
    return m.group(1) if m else None


def _get_col(row: Dict[str, str], candidates: set, default: str = "") -> str:
    for c in candidates:
        if c in row and row[c] != "":
            return row[c]
    return default


def fetch_sheet_csv(sheet_id: str, tab_name: str = "", gid: Optional[str] = None) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """
    Fetches a tab from Google Sheet as CSV (either by tab_name or by gid).
    Returns (rows, error_message).
    """
    if gid is not None:
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&gid={gid}"
        label = f"gid={gid}"
    else:
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={tab_name}"
        label = tab_name
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(url)
            if resp.status_code in (401, 403):
                return [], (
                    "Google Sheet đang để chế độ 'Hạn chế (Restricted)'. "
                    "Bạn hãy mở Google Sheet -> bấm nút 'Chia sẻ (Share)' ở góc phải trên -> "
                    "chọn 'Bất kỳ ai có đường liên kết (Anyone with the link)' -> quyền 'Người xem (Viewer)'."
                )
            if resp.status_code != 200:
                return [], f"Không thể tải tab '{label}' (HTTP {resp.status_code})."

            content = resp.text
            # If Google redirected to HTML login page
            if "<html" in content.lower() or "google.com/accounts" in content.lower():
                return [], (
                    "Google Sheet chưa được mở quyền xem công khai. "
                    "Vui lòng bấm nút 'Chia sẻ (Share)' trên Google Sheet -> chọn 'Bất kỳ ai có đường liên kết đều có thể xem'."
                )

            f = io.StringIO(content)
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return [], f"Tab '{label}' không có dữ liệu hoặc hàng tiêu đề rỗng."

            rows = []
            for row in reader:
                normalized_row = {k.strip().lower(): (v.strip() if v else "") for k, v in row.items() if k}
                rows.append(normalized_row)
            return rows, None
    except Exception as e:
        return [], f"Lỗi kết nối khi tải tab '{label}': {str(e)}"


def validate_page_id(pid_str: str) -> Tuple[Optional[str], Optional[str]]:
    """Validates Page ID string, rejecting scientific notation and non-digits."""
    pid = pid_str.strip().strip("'")
    if "e+" in pid.lower() or "e-" in pid.lower():
        return None, (
            f"Page ID '{pid}' bị lỗi định dạng hàm mũ (E+). "
            f"Hãy thêm dấu nháy đơn ' ở đầu ID trong Sheet để định dạng Văn bản thuần."
        )
    if not re.match(r'^[0-9]{5,30}$', pid):
        return None, f"Page ID '{pid}' không hợp lệ (phải là dãy 5-30 chữ số nguyên vẹn)."
    return pid, None


def validate_spintax_braces(text: str) -> bool:
    """Checks if curly braces { and } are properly balanced and non-empty."""
    depth = 0
    for ch in text:
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth < 0:
                return False
    if depth != 0:
        return False
    if "{}" in text:
        return False
    return True


def _is_unified_single_sheet(rows: List[Dict[str, str]]) -> bool:
    """
    Returns True if a single sheet tab contains BOTH page schedule columns
    (delay / limit) AND comment template columns (All-in-One 1-tab layout).
    """
    if not rows:
        return False
    headers = set(rows[0].keys())
    has_identity = bool(headers & (PAGE_ID_COLS | PAGE_NAME_COLS))
    has_schedule = bool(headers & (DELAY_COLS | LIMIT_COLS))
    has_comment = bool(headers & COMMENT_TPL_COLS)
    return has_identity and has_schedule and has_comment


def _check_pages_headers(rows: List[Dict[str, str]]) -> Optional[str]:
    if not rows:
        return None
    headers = set(rows[0].keys())
    if not (headers & (PAGE_ID_COLS | PAGE_NAME_COLS)):
        return "Tab 'Pages' thiếu cột bắt buộc 'Page ID' (hoặc 'Tên Fanpage')."
    if headers & COMMENT_TPL_COLS and not (headers & DELAY_COLS):
        return "Không tìm thấy tab 'Pages' (Google trả về nhầm tab 'Comments'). Hãy kiểm tra lại tên tab 'Pages'."
    if not (headers & PAGE_CONFIG_COLS):
        return "Tab 'Pages' thiếu các cột cấu hình ('Delay (phút)', 'Bật tự động', ...)."
    return None


def _check_comments_headers(rows: List[Dict[str, str]]) -> Optional[str]:
    if not rows:
        return None
    headers = set(rows[0].keys())
    if not (headers & (PAGE_ID_COLS | PAGE_NAME_COLS)):
        return "Tab 'Comments' thiếu cột bắt buộc 'Page ID'."
    if not (headers & COMMENT_TPL_COLS):
        return (
            "Không tìm thấy tab 'Comments' hợp lệ hoặc thiếu cột 'Mẫu bình luận' "
            "(Google có thể đã trả về nhầm tab đầu tiên do sai tên tab)."
        )
    return None


def sync_from_google_sheet(sheet_url_or_id: str) -> Dict[str, Any]:
    """
    Synchronizes Pages and Comments from Google Sheet into local database inside a single transaction.
    Supports BOTH:
      1. Unified 1-Tab mode (e.g. a single tab with `gid=...` containing Page ID/Name, Delay, Daily Limit, and Comment Templates).
      2. Classic 2-Tab mode (`Pages` tab + `Comments` tab).
    Does NOT require tokens in the Google Sheet (tokens/Antidetect profiles are managed securely in tool).
    """
    sheet_id = extract_sheet_id(sheet_url_or_id)
    if not sheet_id:
        return {"success": False, "error": "Đường link Google Sheet không hợp lệ."}

    gid = extract_sheet_gid(sheet_url_or_id)
    errors: List[str] = []
    warnings: List[str] = []

    # Build lookup maps for connected pages in DB (by page_id, fb_profile_id, and normalized page_name)
    all_db_pages = db.get_all_pages()
    existing_pages_map: Dict[str, Dict[str, Any]] = {}
    existing_by_name: Dict[str, Dict[str, Any]] = {}
    for p in all_db_pages:
        existing_pages_map[p["page_id"]] = p
        if p.get("fb_profile_id"):
            existing_pages_map[str(p["fb_profile_id"])] = p
        pname_norm = (p.get("page_name") or "").strip().lower()
        if pname_norm and pname_norm not in existing_by_name:
            existing_by_name[pname_norm] = p

    # 1. Fetch primary tab (`Pages`, or fallback to `gid` if provided)
    pages_rows, err_pages = fetch_sheet_csv(sheet_id, "Pages")
    if err_pages and gid:
        pages_rows, err_pages = fetch_sheet_csv(sheet_id, gid=gid)
    if err_pages:
        pages_rows, err_pages2 = fetch_sheet_csv(sheet_id, "Danh_Sach_Page")
        if err_pages2:
            return {"success": False, "error": err_pages}

    # If the user passed a specific gid and that gid is a unified 1-tab sheet, prefer it!
    if gid and not _is_unified_single_sheet(pages_rows):
        gid_rows, gid_err = fetch_sheet_csv(sheet_id, gid=gid)
        if not gid_err and _is_unified_single_sheet(gid_rows):
            pages_rows = gid_rows

    if not pages_rows:
        return {"success": False, "error": "Tab dữ liệu trên Google Sheet đang rỗng, hệ thống giữ nguyên cấu hình cũ để an toàn."}

    # Check if this is a Unified 1-Tab Sheet (All-in-One: Page + Delay + Daily Limit + Comment Template in 1 tab)
    unified_mode = _is_unified_single_sheet(pages_rows)

    comments_by_page: Dict[str, List[str]] = {}

    if not unified_mode:
        header_err_pages = _check_pages_headers(pages_rows)
        if header_err_pages:
            return {"success": False, "error": header_err_pages}

        # 2. Fetch Comments tab in 2-tab mode
        comments_rows, err_comments = fetch_sheet_csv(sheet_id, "Comments")
        if err_comments:
            comments_rows, err_comments2 = fetch_sheet_csv(sheet_id, "Mau_Binh_Luan")
            if err_comments2:
                return {"success": False, "error": err_comments}

        if comments_rows:
            header_err_comments = _check_comments_headers(comments_rows)
            if header_err_comments:
                return {"success": False, "error": header_err_comments}

        for idx, r in enumerate(comments_rows or [], start=2):
            pid_raw = _get_col(r, PAGE_ID_COLS)
            pname_raw = _get_col(r, PAGE_NAME_COLS)
            tpl = _get_col(r, COMMENT_TPL_COLS)
            is_active_tpl = _get_col(r, ENABLED_COLS, "true").lower() in ["true", "1", "bật", "yes", "ok", "có"]

            if not pid_raw and not pname_raw and not tpl:
                continue
            if (not pid_raw and not pname_raw) or not tpl:
                errors.append(f"Tab Comments dòng {idx}: Thiếu Page ID hoặc Mẫu bình luận.")
                continue

            clean_pid = None
            if pid_raw:
                clean_pid, pid_err = validate_page_id(str(pid_raw))
                if pid_err:
                    errors.append(f"Tab Comments dòng {idx}: {pid_err}")
                    continue
            elif pname_raw.strip().lower() in existing_by_name:
                clean_pid = existing_by_name[pname_raw.strip().lower()]["page_id"]

            if not validate_spintax_braces(tpl):
                errors.append(f"Tab Comments dòng {idx}: Mẫu bình luận bị lỗi đóng/mở ngoặc nhọn Spintax '{{}}'.")
                continue

            if is_active_tpl and clean_pid:
                comments_by_page.setdefault(clean_pid, []).append(tpl)

    # Parse Pages rows (in unified mode, multiple rows for the same page accumulate templates!)
    seen_sheet_pids = set()
    aggregated_configs: Dict[str, Dict[str, Any]] = {}

    for idx, r in enumerate(pages_rows, start=2):
        pid_raw = _get_col(r, PAGE_ID_COLS)
        pname_raw = _get_col(r, PAGE_NAME_COLS)
        if not pid_raw and not pname_raw:
            continue

        matched_page = None
        clean_pid = None
        if pid_raw:
            clean_pid, pid_err = validate_page_id(str(pid_raw))
            if pid_err:
                errors.append(f"Dòng {idx}: {pid_err}")
                continue
            matched_page = existing_pages_map.get(clean_pid)

        if not matched_page and pname_raw:
            matched_page = existing_by_name.get(pname_raw.strip().lower())
            if matched_page:
                clean_pid = matched_page["page_id"]

        if not matched_page:
            label_missing = clean_pid or pname_raw
            warnings.append(
                f"Page '{label_missing}' (dòng {idx}) có trên Sheet nhưng chưa được kết nối trong Tool. "
                f"Hãy bấm 'Đồng bộ từ Antidetect' hoặc 'Kết nối Nick' trước."
            )
            continue

        canonical_pid = matched_page["page_id"]
        seen_sheet_pids.add(canonical_pid)

        # Parse delay minutes strictly
        delay_raw = _get_col(r, DELAY_COLS, "10").strip()
        if not re.match(r'^[0-9]+$', str(delay_raw)):
            errors.append(f"Dòng {idx}: Giá trị Delay '{delay_raw}' không phải là số nguyên hợp lệ.")
            continue
        delay_minutes = int(delay_raw)
        if delay_minutes < 1 or delay_minutes > 1440:
            errors.append(f"Dòng {idx}: Số phút delay ({delay_minutes}) phải từ 1 đến 1440.")
            continue

        # Parse max comments per day strictly
        limit_raw = _get_col(r, LIMIT_COLS, "20").strip()
        if not re.match(r'^[0-9]+$', str(limit_raw)):
            errors.append(f"Dòng {idx}: Giá trị Giới hạn/ngày '{limit_raw}' không hợp lệ.")
            continue
        max_per_day = int(limit_raw)
        if max_per_day < 1 or max_per_day > 1000:
            errors.append(f"Dòng {idx}: Giới hạn comment/ngày ({max_per_day}) phải từ 1 đến 1000.")
            continue

        # Parse is_enabled
        enabled_val = _get_col(r, ENABLED_COLS, "true").strip().lower()
        is_enabled = enabled_val in ["true", "1", "bật", "yes", "ok", "có"]

        if unified_mode:
            tpl = _get_col(r, COMMENT_TPL_COLS).strip()
            if tpl:
                if not validate_spintax_braces(tpl):
                    errors.append(f"Dòng {idx}: Mẫu bình luận bị lỗi đóng/mở ngoặc nhọn Spintax '{{}}'.")
                    continue
                comments_by_page.setdefault(canonical_pid, []).append(tpl)

        aggregated_configs[canonical_pid] = {
            "matched_page": matched_page,
            "clean_pid": clean_pid,
            "delay_minutes": delay_minutes,
            "max_per_day": max_per_day,
            "is_enabled": is_enabled,
        }

    updates_to_apply: List[Tuple[str, int, List[str], int, bool]] = []
    for canonical_pid, cfg in aggregated_configs.items():
        clean_pid = cfg["clean_pid"]
        matched_page = cfg["matched_page"]
        templates = comments_by_page.get(canonical_pid) or (comments_by_page.get(clean_pid) if clean_pid else None) or []
        if not templates:
            try:
                curr = matched_page.get("templates")
                templates = json.loads(curr) if curr else []
            except Exception:
                templates = []
            if not templates:
                templates = ["Dạ chào bạn, inbox page để được tư vấn chi tiết nhé!"]

        updates_to_apply.append((
            canonical_pid,
            cfg["delay_minutes"],
            templates,
            cfg["max_per_day"],
            cfg["is_enabled"],
        ))

    # Apply all updates inside a single DB transaction
    synced_count = 0
    disabled_count = 0
    conn = db.get_connection()
    try:
        with conn:
            for (canonical_pid, delay_minutes, templates, max_per_day, is_enabled) in updates_to_apply:
                db.update_page_rule(
                    page_id=canonical_pid,
                    delay_minutes=delay_minutes,
                    templates=templates,
                    max_comments_per_day=max_per_day,
                    is_enabled=is_enabled,
                    conn=conn
                )
                synced_count += 1

            # Soft-disable any previously active pages that were removed from the Sheet
            # Only do this if there were no row-level parsing errors on the Pages tab
            if seen_sheet_pids and not errors:
                for pdata in all_db_pages:
                    pid = pdata["page_id"]
                    if pid not in seen_sheet_pids and pdata.get("rule_enabled") == 1:
                        db.update_page_rule(
                            page_id=pid,
                            delay_minutes=pdata.get("delay_minutes", 10),
                            templates=json.loads(pdata.get("templates") or "[]"),
                            max_comments_per_day=pdata.get("max_comments_per_day", 20),
                            is_enabled=False,
                            conn=conn
                        )
                        disabled_count += 1
    finally:
        conn.close()

    mode_str = " (Chế độ 1 Bảng gộp)" if unified_mode else " (Chế độ 2 Tab Pages & Comments)"
    msg = f"Đã đồng bộ thành công {synced_count} Fanpage từ Google Sheet{mode_str}."
    if disabled_count > 0:
        msg += f" Đã tạm dừng {disabled_count} Page không còn nằm trong Sheet."
    if errors:
        msg += f" Phát hiện {len(errors)} dòng lỗi cần kiểm tra."

    return {
        "success": True,
        "message": msg,
        "pages_synced": synced_count,
        "pages_disabled": disabled_count,
        "errors": errors,
        "warnings": warnings
    }
