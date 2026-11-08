import re
import csv
import io
import json
import unicodedata
import httpx
from typing import Dict, Any, List, Optional, Tuple

import database as db

PAGE_ID_COLS = {"page id", "page_id", "id page", "id trang", "id", "mã trang", "ma trang"}
PAGE_NAME_COLS = {
    "tên gợi nhớ", "page name", "page_name", "tên page", "ten page",
    "tên fanpage", "ten fanpage", "fanpage", "tên trang", "ten trang"
}
COMMENT_TPL_COLS = {
    "mẫu bình luận", "mau binh luan", "comment", "comments", "template",
    "comment_template", "nội dung comment", "noi dung comment", "nội dung bình luận", "binh luan"
}
DELAY_COLS = {
    "delay (phút)", "delay", "thời gian chờ", "delay_minutes",
    "sau bao lâu comment (phút)", "sau bao lâu comment", "sau bao lau comment",
    "so phut cho", "số phút chờ", "thời gian trễ", "phút chờ"
}
LIMIT_COLS = {
    "giới hạn comment/ngày", "giới hạn", "max_comments_per_day",
    "mỗi ngày mấy comment", "moi ngay may comment", "số comment/ngày", "so comment/ngay", "số bình luận/ngày"
}
ENABLED_COLS = {"bật tự động", "bật/tắt", "enabled", "bật", "is_enabled", "trạng thái"}

PAGE_CONFIG_COLS = DELAY_COLS | LIMIT_COLS | ENABLED_COLS | PAGE_NAME_COLS

# Common shorthand aliases used in the user's Google Sheet
SLUG_ALIASES = {
    "taphoadoll": "tiemdollxinhxan",
}


def _normalize_slug(text: str) -> str:
    """Strips Vietnamese diacritics, spaces, and punctuation for fuzzy Page name/slug matching."""
    if not text:
        return ""
    s = str(text).strip().lower().replace("đ", "d")
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", "", s)


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


def _discover_sheet_gids(sheet_id: str) -> List[str]:
    """Discovers non-zero GIDs from a public Google Sheet HTML page when gid=0 is empty."""
    gids: List[str] = ["1405549804"]
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            resp = client.get(f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
            if resp.status_code == 200:
                found = re.findall(r'gid["\s:=]+(\d{5,})', resp.text)
                for g in found:
                    if g not in gids and g != "0":
                        gids.append(g)
    except Exception:
        pass
    return gids


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
                normalized_row = {
                    k.strip().lower(): (v.strip() if v else "")
                    for k, v in row.items()
                    if k and k.strip()
                }
                if any(normalized_row.values()):
                    rows.append(normalized_row)
            return rows, None
    except Exception as e:
        return [], f"Lỗi kết nối khi tải tab '{label}': {str(e)}"


def validate_page_id(pid_str: str) -> Tuple[Optional[str], Optional[str]]:
    """Validates numeric Page ID string, rejecting scientific notation and non-digits."""
    pid = pid_str.strip().strip("'")
    if re.search(r'^\d+(?:[.,]\d+)?e[+-]\d+$', pid.lower()):
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
    Returns True if a single sheet tab contains BOTH Page identity column(s)
    (`ID trang` / `Page ID` / `Tên Fanpage`) AND Comment column(s) (`Comment` / `Mẫu bình luận`).
    """
    if not rows:
        return False
    headers = set(rows[0].keys())
    has_identity = bool(headers & (PAGE_ID_COLS | PAGE_NAME_COLS))
    has_comment = bool(headers & COMMENT_TPL_COLS)
    return has_identity and has_comment


def _check_pages_headers(rows: List[Dict[str, str]]) -> Optional[str]:
    if not rows:
        return None
    headers = set(rows[0].keys())
    if not (headers & (PAGE_ID_COLS | PAGE_NAME_COLS)):
        return "Tab 'Pages' thiếu cột bắt buộc 'Page ID' (hoặc 'Tên Fanpage')."
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


def _resolve_page_from_identifier(
    raw_id_or_name: str,
    raw_name: str,
    existing_pages_map: Dict[str, Dict[str, Any]],
    existing_by_name: Dict[str, Dict[str, Any]],
    existing_by_slug: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    """
    Resolves a row's Page identifier (which can be a numeric Page ID, an accented Page Name
    like 'Tiệm Tạp Hóa Decor', or a compact slug like 'Chidepthichve') to a DB Page record.
    Returns `(matched_page_dict, clean_id_or_label, validation_error_if_corrupt_number)`.
    """
    cand = (raw_id_or_name or "").strip().strip("'")
    name_cand = (raw_name or "").strip()

    # 1. Check scientific notation corruption (e.g. 1.02938E+14)
    if re.search(r'^\d+(?:[.,]\d+)?e[+-]\d+$', cand.lower()):
        _, err = validate_page_id(cand)
        return None, None, err

    # 2. If pure digits, validate and look up by ID
    if cand.isdigit():
        clean_pid, err = validate_page_id(cand)
        if err:
            return None, None, err
        return existing_pages_map.get(clean_pid), clean_pid, None

    # 3. Try matching `cand` or `name_cand` by exact lowercase name or normalized slug
    for text in (cand, name_cand):
        if not text:
            continue
        low = text.lower().strip()
        if low in existing_by_name:
            p = existing_by_name[low]
            return p, p["page_id"], None
        slug = _normalize_slug(text)
        if slug in SLUG_ALIASES:
            slug = SLUG_ALIASES[slug]
        if slug and slug in existing_by_slug:
            p = existing_by_slug[slug]
            return p, p["page_id"], None

    return None, (cand or name_cand), None


def sync_from_google_sheet(sheet_url_or_id: str) -> Dict[str, Any]:
    """
    Synchronizes Pages and Comments from Google Sheet into local database inside a single transaction.
    Supports BOTH:
      1. Unified 1-Tab mode (e.g. `ID trang` + `Comment` + optional `Sau bao lâu comment (phút)` + `Mỗi ngày mấy comment`).
      2. Classic 2-Tab mode (`Pages` tab + `Comments` tab).
    """
    sheet_id = extract_sheet_id(sheet_url_or_id)
    if not sheet_id:
        return {"success": False, "error": "Đường link Google Sheet không hợp lệ."}

    gid = extract_sheet_gid(sheet_url_or_id)
    errors: List[str] = []
    warnings: List[str] = []

    # Build lookup maps for connected pages in DB (by page_id, fb_profile_id, lowercase name, and unaccented slug)
    all_db_pages = db.get_all_pages()
    existing_pages_map: Dict[str, Dict[str, Any]] = {}
    existing_by_name: Dict[str, Dict[str, Any]] = {}
    existing_by_slug: Dict[str, Dict[str, Any]] = {}
    for p in all_db_pages:
        existing_pages_map[p["page_id"]] = p
        if p.get("fb_profile_id"):
            existing_pages_map[str(p["fb_profile_id"])] = p
        pname = (p.get("page_name") or "").strip()
        if pname:
            low = pname.lower()
            if low not in existing_by_name:
                existing_by_name[low] = p
            slug = _normalize_slug(pname)
            if slug and slug not in existing_by_slug:
                existing_by_slug[slug] = p

    # 1. Fetch primary tab: if `gid` is in URL, try that `gid` first; else try `Pages`, then discovered GIDs
    pages_rows: List[Dict[str, str]] = []
    err_pages: Optional[str] = None

    if gid:
        pages_rows, err_pages = fetch_sheet_csv(sheet_id, gid=gid)

    if not pages_rows:
        pages_rows, err_pages = fetch_sheet_csv(sheet_id, "Pages")
        if err_pages and "không có dữ liệu" not in err_pages:
            pages_rows, err_pages2 = fetch_sheet_csv(sheet_id, "Danh_Sach_Page")
            if err_pages2 and "không có dữ liệu" not in err_pages2:
                return {"success": False, "error": err_pages}

    # If the first tab (gid=0) is blank, automatically check non-zero GIDs in the spreadsheet
    if not pages_rows:
        for cand_gid in _discover_sheet_gids(sheet_id):
            cand_rows, cand_err = fetch_sheet_csv(sheet_id, gid=cand_gid)
            if not cand_err and cand_rows:
                pages_rows = cand_rows
                break


    if not pages_rows:
        return {"success": False, "error": "Tab dữ liệu trên Google Sheet đang rỗng, hệ thống giữ nguyên cấu hình cũ để an toàn."}

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

            matched_p, clean_pid, pid_err = _resolve_page_from_identifier(
                pid_raw, pname_raw, existing_pages_map, existing_by_name, existing_by_slug
            )
            if pid_err:
                errors.append(f"Tab Comments dòng {idx}: {pid_err}")
                continue

            if not validate_spintax_braces(tpl):
                errors.append(f"Tab Comments dòng {idx}: Mẫu bình luận bị lỗi đóng/mở ngoặc nhọn Spintax '{{}}'.")
                continue

            target_key = matched_p["page_id"] if matched_p else clean_pid
            if is_active_tpl and target_key:
                comments_by_page.setdefault(target_key, []).append(tpl)

    # Parse Pages rows (in unified mode, multiple rows for the same page accumulate templates!)
    seen_sheet_pids = set()
    aggregated_configs: Dict[str, Dict[str, Any]] = {}

    for idx, r in enumerate(pages_rows, start=2):
        pid_raw = _get_col(r, PAGE_ID_COLS)
        pname_raw = _get_col(r, PAGE_NAME_COLS)
        if not pid_raw and not pname_raw:
            continue

        matched_page, clean_pid, pid_err = _resolve_page_from_identifier(
            pid_raw, pname_raw, existing_pages_map, existing_by_name, existing_by_slug
        )
        if pid_err:
            errors.append(f"Dòng {idx}: {pid_err}")
            continue

        if not matched_page:
            label_missing = clean_pid or pid_raw or pname_raw
            warnings.append(
                f"Page '{label_missing}' (dòng {idx}) có trên Sheet nhưng chưa tìm thấy Page khớp tên/ID trong Tool."
            )
            continue

        canonical_pid = matched_page["page_id"]
        seen_sheet_pids.add(canonical_pid)

        # Parse delay minutes (if provided in row; otherwise keep existing Page delay or default 10)
        delay_col_val = _get_col(r, DELAY_COLS, "").strip()
        if delay_col_val:
            if not re.match(r'^[0-9]+$', str(delay_col_val)):
                errors.append(f"Dòng {idx}: Giá trị Delay '{delay_col_val}' không phải là số nguyên hợp lệ.")
                continue
            delay_minutes = int(delay_col_val)
            if delay_minutes < 1 or delay_minutes > 1440:
                errors.append(f"Dòng {idx}: Số phút delay ({delay_minutes}) phải từ 1 đến 1440.")
                continue
        else:
            prev_cfg = aggregated_configs.get(canonical_pid)
            delay_minutes = prev_cfg["delay_minutes"] if prev_cfg else (matched_page.get("delay_minutes") or 10)

        # Parse max comments per day (if provided in row; otherwise keep existing Page limit or default 20)
        limit_col_val = _get_col(r, LIMIT_COLS, "").strip()
        if limit_col_val:
            if not re.match(r'^[0-9]+$', str(limit_col_val)):
                errors.append(f"Dòng {idx}: Giá trị Giới hạn/ngày '{limit_col_val}' không hợp lệ.")
                continue
            max_per_day = int(limit_col_val)
            if max_per_day < 1 or max_per_day > 1000:
                errors.append(f"Dòng {idx}: Giới hạn comment/ngày ({max_per_day}) phải từ 1 đến 1000.")
                continue
        else:
            prev_cfg = aggregated_configs.get(canonical_pid)
            max_per_day = prev_cfg["max_per_day"] if prev_cfg else (matched_page.get("max_comments_per_day") or 20)

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

            # In classic 2-tab mode, soft-disable previously active pages removed from the Pages tab
            if not unified_mode and seen_sheet_pids and not errors:
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

    total_templates = sum(len(t[2]) for t in updates_to_apply)
    mode_str = f" (Chế độ 1 Bảng gộp — tổng {total_templates} mẫu bình luận)" if unified_mode else " (Chế độ 2 Tab Pages & Comments)"
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
