import re
import csv
import io
import json
import httpx
from typing import Dict, Any, List, Optional, Tuple

import database as db
import spintax

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
                return [], "Google Sheet chưa được cấp quyền công khai. Vui lòng chọn 'Bất kỳ ai có đường liên kết đều có thể xem'."

            f = io.StringIO(content)
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return [], f"Tab '{tab_name}' không có dữ liệu hoặc hàng tiêu đề rỗng."

            rows = []
            for row in reader:
                normalized_row = {k.strip().lower(): v.strip() for k, v in row.items() if k}
                rows.append(normalized_row)
            return rows, None
    except Exception as e:
        return [], f"Lỗi kết nối khi tải tab '{tab_name}': {str(e)}"

def validate_page_id(pid_str: str) -> Tuple[Optional[str], Optional[str]]:
    """Validates Page ID string, rejecting scientific notation and non-digits."""
    pid = pid_str.strip().strip("'")
    if "e+" in pid.lower() or "e-" in pid.lower():
        return None, f"Page ID '{pid}' bị lỗi định dạng hàm mũ (E+). Hãy thêm dấu nháy đơn ' ở đầu ID trong Sheet để định dạng Văn bản thuần."
    clean_digits = re.sub(r'[^0-9]', '', pid)
    if len(clean_digits) < 5 or len(clean_digits) > 30:
        return None, f"Page ID '{pid}' không hợp lệ (phải là dãy 5-30 chữ số)."
    return clean_digits, None

def validate_spintax_braces(text: str) -> bool:
    """Checks if curly braces { and } are properly balanced."""
    depth = 0
    for ch in text:
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth < 0:
                return False
    return depth == 0

def sync_from_google_sheet(sheet_url_or_id: str) -> Dict[str, Any]:
    """
    Synchronizes Pages and Comments from Google Sheet into local database.
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
        # Try fallback name
        pages_rows, err_pages2 = fetch_sheet_csv(sheet_id, "Danh_Sach_Page")
        if err_pages2:
            return {"success": False, "error": err_pages}

    # 2. Fetch Comments tab
    comments_rows, err_comments = fetch_sheet_csv(sheet_id, "Comments")
    if err_comments:
        comments_rows, _ = fetch_sheet_csv(sheet_id, "Mau_Binh_Luan")

    # Group comments by page_id
    comments_by_page: Dict[str, List[str]] = {}
    for idx, r in enumerate(comments_rows or [], start=2):
        pid_raw = r.get("page id") or r.get("page_id") or r.get("id page") or ""
        tpl = r.get("mẫu bình luận") or r.get("mau binh luan") or r.get("comment") or r.get("template") or ""
        is_active_tpl = str(r.get("bật") or r.get("is_enabled") or "true").lower() in ["true", "1", "bật", "yes"]

        if not pid_raw or not tpl:
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

    # Fetch currently connected pages in DB
    existing_pages_map = {p["page_id"]: p for p in db.get_all_pages()}
    synced_count = 0
    seen_sheet_pids = set()

    for idx, r in enumerate(pages_rows, start=2):
        pid_raw = r.get("page id") or r.get("page_id") or r.get("id page") or ""
        if not pid_raw:
            continue

        clean_pid, pid_err = validate_page_id(str(pid_raw))
        if pid_err:
            errors.append(f"Tab Pages dòng {idx}: {pid_err}")
            continue

        seen_sheet_pids.add(clean_pid)

        # Check if Page is already connected in Tool
        if clean_pid not in existing_pages_map:
            warnings.append(f"Page ID '{clean_pid}' (dòng {idx}) có trên Sheet nhưng chưa được kết nối trong Tool. Hãy bấm 'Kết nối Nick' để nạp token trước.")
            continue

        # Parse delay minutes
        delay_raw = r.get("delay (phút)") or r.get("delay") or r.get("thời gian chờ") or "10"
        try:
            delay_minutes = int(re.sub(r'[^0-9]', '', str(delay_raw)))
            if delay_minutes < 1 or delay_minutes > 1440:
                errors.append(f"Tab Pages dòng {idx}: Số phút delay phải từ 1 đến 1440.")
                continue
        except Exception:
            delay_minutes = 10

        # Parse max comments per day
        limit_raw = r.get("giới hạn comment/ngày") or r.get("giới hạn") or r.get("max_comments_per_day") or "20"
        try:
            max_per_day = int(re.sub(r'[^0-9]', '', str(limit_raw)))
        except Exception:
            max_per_day = 20

        # Parse is_enabled
        enabled_val = str(r.get("bật tự động") or r.get("bật/tắt") or r.get("enabled") or "true").lower()
        is_enabled = enabled_val in ["true", "1", "bật", "yes", "ok"]

        templates = comments_by_page.get(clean_pid, [])
        if not templates:
            # Keep existing templates if Sheet doesn't specify any for this page
            try:
                curr = existing_pages_map[clean_pid].get("templates")
                templates = json.loads(curr) if curr else []
            except Exception:
                templates = []
            if not templates:
                templates = ["Dạ chào bạn, inbox page để được tư vấn chi tiết nhé!"]

        # Update rule in DB
        db.update_page_rule(
            page_id=clean_pid,
            delay_minutes=delay_minutes,
            templates=templates,
            max_comments_per_day=max_per_day,
            is_enabled=is_enabled
        )
        synced_count += 1

    # Soft-disable any previously active pages that were removed from the Sheet
    disabled_count = 0
    if seen_sheet_pids:
        for pid, pdata in existing_pages_map.items():
            if pid not in seen_sheet_pids and pdata.get("rule_enabled") == 1:
                # Turn off rule
                db.update_page_rule(
                    page_id=pid,
                    delay_minutes=pdata.get("delay_minutes", 10),
                    templates=json.loads(pdata.get("templates") or "[]"),
                    max_comments_per_day=pdata.get("max_comments_per_day", 20),
                    is_enabled=False
                )
                disabled_count += 1

    msg = f"Đã đồng bộ thành công {synced_count} Fanpage từ Google Sheet."
    if disabled_count > 0:
        msg += f" Đã tạm dừng {disabled_count} Page không còn nằm trong Sheet."

    return {
        "success": True,
        "message": msg,
        "pages_synced": synced_count,
        "pages_disabled": disabled_count,
        "errors": errors,
        "warnings": warnings
    }
