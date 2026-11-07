import re
import csv
import io
import json
import httpx
from typing import Dict, Any, List, Optional, Tuple

import database as db

PAGE_ID_COLS = {"page id", "page_id", "id page"}
COMMENT_TPL_COLS = {"mẫu bình luận", "mau binh luan", "comment", "template"}
PAGE_CONFIG_COLS = {
    "delay (phút)", "delay", "thời gian chờ",
    "bật tự động", "bật/tắt", "enabled",
    "tên gợi nhớ", "page name", "page_name", "tên page",
    "giới hạn comment/ngày", "giới hạn", "max_comments_per_day"
}


def extract_sheet_id(url_or_id: str) -> Optional[str]:
    url_or_id = url_or_id.strip()
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', url_or_id)
    if match:
        return match.group(1)
    if re.match(r'^[a-zA-Z0-9-_]{20,}$', url_or_id):
        return url_or_id
    return None


def fetch_sheet_csv(sheet_id: str, tab_name: str) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """
    Fetches a tab from Google Sheet as CSV.
    Returns (rows, error_message).
    """
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={tab_name}"
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                return [], f"Không thể tải tab '{tab_name}' (HTTP {resp.status_code})."

            content = resp.text
            # If Google redirected to HTML login page
            if "<html" in content.lower() or "google.com/accounts" in content.lower():
                return [], "Google Sheet chưa được cấp quyền chia sẻ. Vui lòng chọn 'Bất kỳ ai có đường liên kết đều có thể xem'."

            f = io.StringIO(content)
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return [], f"Tab '{tab_name}' không có dữ liệu hoặc hàng tiêu đề rỗng."

            rows = []
            for row in reader:
                normalized_row = {k.strip().lower(): (v.strip() if v else "") for k, v in row.items() if k}
                rows.append(normalized_row)
            return rows, None
    except Exception as e:
        return [], f"Lỗi kết nối khi tải tab '{tab_name}': {str(e)}"


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


def _check_pages_headers(rows: List[Dict[str, str]]) -> Optional[str]:
    if not rows:
        return None
    headers = set(rows[0].keys())
    if not (headers & PAGE_ID_COLS):
        return "Tab 'Pages' thiếu cột bắt buộc 'Page ID'."
    if headers & COMMENT_TPL_COLS and not (headers & {"delay (phút)", "delay", "thời gian chờ"}):
        return "Không tìm thấy tab 'Pages' (Google trả về nhầm tab 'Comments'). Hãy kiểm tra lại tên tab 'Pages'."
    if not (headers & PAGE_CONFIG_COLS):
        return "Tab 'Pages' thiếu các cột cấu hình ('Delay (phút)', 'Bật tự động', ...)."
    return None


def _check_comments_headers(rows: List[Dict[str, str]]) -> Optional[str]:
    if not rows:
        return None
    headers = set(rows[0].keys())
    if not (headers & PAGE_ID_COLS):
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
    Does NOT require tokens in the Google Sheet (tokens are managed securely in tool).
    """
    sheet_id = extract_sheet_id(sheet_url_or_id)
    if not sheet_id:
        return {"success": False, "error": "Đường link Google Sheet không hợp lệ."}

    errors: List[str] = []
    warnings: List[str] = []

    # 1. Fetch Pages tab
    pages_rows, err_pages = fetch_sheet_csv(sheet_id, "Pages")
    if err_pages:
        pages_rows, err_pages2 = fetch_sheet_csv(sheet_id, "Danh_Sach_Page")
        if err_pages2:
            return {"success": False, "error": err_pages}

    if not pages_rows:
        return {"success": False, "error": "Tab 'Pages' rỗng, hệ thống giữ nguyên cấu hình cũ để an toàn."}

    header_err_pages = _check_pages_headers(pages_rows)
    if header_err_pages:
        return {"success": False, "error": header_err_pages}

    # 2. Fetch Comments tab
    comments_rows, err_comments = fetch_sheet_csv(sheet_id, "Comments")
    if err_comments:
        comments_rows, err_comments2 = fetch_sheet_csv(sheet_id, "Mau_Binh_Luan")
        if err_comments2:
            return {"success": False, "error": err_comments}

    if comments_rows:
        header_err_comments = _check_comments_headers(comments_rows)
        if header_err_comments:
            return {"success": False, "error": header_err_comments}

    # Group comments by page_id
    comments_by_page: Dict[str, List[str]] = {}
    for idx, r in enumerate(comments_rows or [], start=2):
        pid_raw = r.get("page id") or r.get("page_id") or r.get("id page") or ""
        tpl = r.get("mẫu bình luận") or r.get("mau binh luan") or r.get("comment") or r.get("template") or ""
        is_active_tpl = str(r.get("bật") or r.get("is_enabled") or "true").lower() in ["true", "1", "bật", "yes", "ok"]

        if not pid_raw and not tpl:
            continue
        if not pid_raw or not tpl:
            errors.append(f"Tab Comments dòng {idx}: Thiếu Page ID hoặc Mẫu bình luận.")
            continue

        clean_pid, pid_err = validate_page_id(str(pid_raw))
        if pid_err:
            errors.append(f"Tab Comments dòng {idx}: {pid_err}")
            continue

        if not validate_spintax_braces(tpl):
            errors.append(f"Tab Comments dòng {idx}: Mẫu bình luận bị lỗi đóng/mở ngoặc nhọn Spintax '{{}}'.")
            continue

        if is_active_tpl:
            if clean_pid not in comments_by_page:
                comments_by_page[clean_pid] = []
            comments_by_page[clean_pid].append(tpl)

    # Fetch currently connected pages in DB (map both page_id and fb_profile_id -> canonical page)
    all_db_pages = db.get_all_pages()
    existing_pages_map = {}
    for p in all_db_pages:
        existing_pages_map[p["page_id"]] = p
        if p.get("fb_profile_id"):
            existing_pages_map[str(p["fb_profile_id"])] = p

    seen_sheet_pids = set()
    updates_to_apply: List[Tuple[str, int, List[str], int, bool]] = []

    for idx, r in enumerate(pages_rows, start=2):
        pid_raw = r.get("page id") or r.get("page_id") or r.get("id page") or ""
        if not pid_raw:
            continue

        clean_pid, pid_err = validate_page_id(str(pid_raw))
        if pid_err:
            errors.append(f"Tab Pages dòng {idx}: {pid_err}")
            continue

        if clean_pid not in existing_pages_map:
            warnings.append(
                f"Page ID '{clean_pid}' (dòng {idx}) có trên Sheet nhưng chưa được kết nối trong Tool. "
                f"Hãy bấm 'Đồng bộ từ Antidetect' hoặc 'Kết nối Nick' trước."
            )
            continue

        matched_page = existing_pages_map[clean_pid]
        canonical_pid = matched_page["page_id"]
        seen_sheet_pids.add(canonical_pid)


        # Parse delay minutes strictly
        delay_raw = (r.get("delay (phút)") or r.get("delay") or r.get("thời gian chờ") or "10").strip()
        if not re.match(r'^[0-9]+$', str(delay_raw)):
            errors.append(f"Tab Pages dòng {idx}: Giá trị Delay '{delay_raw}' không phải là số nguyên hợp lệ.")
            continue
        delay_minutes = int(delay_raw)
        if delay_minutes < 1 or delay_minutes > 1440:
            errors.append(f"Tab Pages dòng {idx}: Số phút delay ({delay_minutes}) phải từ 1 đến 1440.")
            continue

        # Parse max comments per day strictly
        limit_raw = (r.get("giới hạn comment/ngày") or r.get("giới hạn") or r.get("max_comments_per_day") or "20").strip()
        if not re.match(r'^[0-9]+$', str(limit_raw)):
            errors.append(f"Tab Pages dòng {idx}: Giá trị Giới hạn/ngày '{limit_raw}' không hợp lệ.")
            continue
        max_per_day = int(limit_raw)
        if max_per_day < 1 or max_per_day > 1000:
            errors.append(f"Tab Pages dòng {idx}: Giới hạn comment/ngày ({max_per_day}) phải từ 1 đến 1000.")
            continue

        # Parse is_enabled
        enabled_val = str(r.get("bật tự động") or r.get("bật/tắt") or r.get("enabled") or "true").strip().lower()
        is_enabled = enabled_val in ["true", "1", "bật", "yes", "ok"]

        templates = comments_by_page.get(clean_pid) or comments_by_page.get(canonical_pid) or []
        if not templates:
            try:
                curr = matched_page.get("templates")
                templates = json.loads(curr) if curr else []
            except Exception:
                templates = []
            if not templates:
                templates = ["Dạ chào bạn, inbox page để được tư vấn chi tiết nhé!"]

        updates_to_apply.append((canonical_pid, delay_minutes, templates, max_per_day, is_enabled))

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

    msg = f"Đã đồng bộ thành công {synced_count} Fanpage từ Google Sheet."
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
