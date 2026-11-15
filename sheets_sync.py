import re
import csv
import io
import json
import unicodedata
import httpx
from typing import Dict, Any, List, Optional, Tuple

import database as db
import comment_content

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


# Avoid inheriting malformed OS proxy settings that crash httpx 0.28 on client init.
def _http_client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False)


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
        with _http_client(10.0) as client:
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


def _get_explicit_col(row: Dict[str, str], candidates: set) -> Optional[str]:
    """Returns the cell value only when the column exists and is non-empty."""
    for c in candidates:
        if c in row:
            value = (row[c] or "").strip()
            if value:
                return value
    return None


TRUE_VALUES = {
    "true", "1", "bật", "bat", "yes", "ok", "có", "co", "on",
    "hoạt động", "hoat dong", "active",
}
FALSE_VALUES = {
    "false", "0", "tắt", "tat", "không", "khong", "no", "off",
    "tạm dừng", "tam dung", "inactive", "khóa", "khoa",
}


def _parse_enabled(value: Optional[str], default: bool) -> Tuple[bool, Optional[str]]:
    if value is None:
        return default, None
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True, None
    if normalized in FALSE_VALUES:
        return False, None
    return default, f"Giá trị 'Bật tự động' '{value}' không hợp lệ (dùng TRUE/FALSE)."


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
        with _http_client(15.0) as client:
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
    ambiguous_names: Optional[set] = None,
    ambiguous_slugs: Optional[set] = None,
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
    ambiguous_label = None
    for text in (cand, name_cand):
        if not text:
            continue
        low = text.lower().strip()
        if ambiguous_names and low in ambiguous_names:
            ambiguous_label = ambiguous_label or text
            continue
        if low in existing_by_name:
            p = existing_by_name[low]
            return p, p["page_id"], None
        slug = _normalize_slug(text)
        if slug in SLUG_ALIASES:
            slug = SLUG_ALIASES[slug]
        if ambiguous_slugs and slug in ambiguous_slugs:
            ambiguous_label = ambiguous_label or text
            continue
        if slug and slug in existing_by_slug:
            p = existing_by_slug[slug]
            return p, p["page_id"], None

    if ambiguous_label:
        return None, (cand or name_cand), f"Tên Page '{ambiguous_label}' đang trùng với nhiều Page trong Tool; vui lòng dùng Page ID để đồng bộ chính xác."
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
    ambiguous_names: set = set()
    ambiguous_slugs: set = set()
    for p in all_db_pages:
        existing_pages_map[p["page_id"]] = p
        if p.get("fb_profile_id"):
            existing_pages_map[str(p["fb_profile_id"])] = p
        pname = (p.get("page_name") or "").strip()
        if pname:
            low = pname.lower()
            if low in existing_by_name:
                if existing_by_name[low]["page_id"] != p["page_id"]:
                    ambiguous_names.add(low)
            else:
                existing_by_name[low] = p
            slug = _normalize_slug(pname)
            if slug:
                if slug in existing_by_slug:
                    if existing_by_slug[slug]["page_id"] != p["page_id"]:
                        ambiguous_slugs.add(slug)
                else:
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
                pid_raw, pname_raw, existing_pages_map, existing_by_name, existing_by_slug,
                ambiguous_names, ambiguous_slugs
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
            pid_raw, pname_raw, existing_pages_map, existing_by_name, existing_by_slug,
            ambiguous_names, ambiguous_slugs
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

        # Parse is_enabled. Column absent/empty => keep current state, so a periodic
        # sync cannot silently re-enable a rule the operator disabled in the Tool.
        prev_cfg = aggregated_configs.get(canonical_pid)
        existing_enabled = (
            prev_cfg["is_enabled"] if prev_cfg and prev_cfg["is_enabled"] is not None
            else bool(matched_page.get("rule_enabled", 1))
        )
        raw_enabled = _get_explicit_col(r, ENABLED_COLS)
        if raw_enabled is not None:
            is_enabled, enabled_err = _parse_enabled(raw_enabled, existing_enabled)
            if enabled_err:
                errors.append(f"Dòng {idx}: {enabled_err}")
                continue
        else:
            is_enabled = prev_cfg["is_enabled"] if prev_cfg else None

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

def pilot_comment_for_post(templates: List[str], post_id: str) -> str:
    """Pick consistently so editing a Sheet row updates the matching queued content."""
    return comment_content.select_template(templates, post_id)


def sync_pilot_comment_sheet(sheet_url: str, page_id: str) -> Dict[str, Any]:
    """Read one exact tab and apply its contents to one Page without changing rule state."""
    from contextlib import closing
    from datetime import datetime, timezone
    from decimal import Decimal, InvalidOperation
    from urllib.parse import urlsplit

    parsed = urlsplit(sheet_url)
    match = re.fullmatch(r"/spreadsheets/d/([A-Za-z0-9_-]+)(?:/edit)?/?", parsed.path)
    gid = extract_sheet_gid(sheet_url)
    if parsed.scheme != "https" or parsed.netloc != "docs.google.com" or not match or not gid or not page_id:
        return {"success": False, "error": "Cấu hình Sheet pilot cần URL Google Sheets có gid và ID Page."}
    url = f"https://docs.google.com/spreadsheets/d/{match.group(1)}/gviz/tq"
    try:
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            response = client.get(url, params={
                "tqx": "out:csv", "gid": gid, "range": "A1:D250", "headers": "1",
            })
        if response.status_code != 200 or "text/csv" not in response.headers.get("content-type", ""):
            return {"success": False, "error": "Không đọc được tab bình luận đã cấu hình."}
        rows = list(csv.reader(io.StringIO(response.text)))
    except (httpx.HTTPError, csv.Error):
        return {"success": False, "error": "Lỗi khi đọc tab bình luận từ Google."}
    if not rows or rows[0] != ["ID trang", "Comment", "Tên Fanpage", "Số giờ chờ sau khi đăng bài"]:
        return {"success": False, "error": "Tab bình luận sai tiêu đề A1:D1."}

    templates: List[str] = []
    delays = set()
    for row_index, row in enumerate(rows[1:], 2):
        pid, comment, _, hours = (row + [""] * 4)[:4]
        if not comment.strip():
            continue
        if pid.strip() != page_id:
            return {"success": False, "error": f"Dòng {row_index} không thuộc Page pilot."}
        if len(comment) > 8000 or re.search(r"\bEA[A-Z][A-Za-z0-9]{30,}\b", comment):
            return {"success": False, "error": f"Dòng {row_index} có nội dung không hợp lệ."}
        if hours.strip():
            try:
                exact = Decimal(hours.strip().replace(",", ".")) * 60
                if not exact.is_finite() or not 0 <= exact <= 1440:
                    raise ValueError
                minute = int(exact.to_integral_value())
                if abs(exact - minute) > Decimal("0.001"):
                    raise ValueError
                delays.add(minute)
            except (InvalidOperation, ValueError, OverflowError):
                return {"success": False, "error": f"Số giờ chờ ở dòng {row_index} không đổi được thành số phút nguyên."}
        if comment.strip() not in templates:
            templates.append(comment.strip())
    if not templates or len(delays) != 1:
        return {"success": False, "error": "Cần ít nhất một mẫu và một thời gian chờ chung."}
    delay = next(iter(delays))
    now = datetime.now(timezone.utc).isoformat()
    changed = 0
    serialized = json.dumps(templates, ensure_ascii=False)
    with closing(db.get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        rule = conn.execute("SELECT delay_minutes,templates FROM page_rules WHERE page_id=?", (page_id,)).fetchone()
        if rule is None:
            return {"success": False, "error": "Không tìm thấy rule của Page pilot."}
        if rule["delay_minutes"] != delay or rule["templates"] != serialized:
            conn.execute(
                "UPDATE page_rules SET delay_minutes=?,templates=?,updated_at=? WHERE page_id=?",
                (delay, serialized, now, page_id),
            )
        pending = conn.execute(
            "SELECT id,post_id,comment_content FROM tasks WHERE page_id=? AND source='AUTO' AND status='PENDING'",
            (page_id,),
        ).fetchall()
        for task in pending:
            content = pilot_comment_for_post(templates, task["post_id"])
            if task["comment_content"] != content:
                conn.execute("UPDATE tasks SET comment_content=?,updated_at=? WHERE id=?", (content, now, task["id"]))
                changed += 1
    return {"success": True, "pages_synced": 1, "templates_count": len(templates), "delay_minutes": delay,
            "pending_updated": changed, "errors": [], "warnings": [],
            "message": f"Đã đồng bộ {len(templates)} mẫu cho Page pilot; cập nhật {changed} tác vụ đang chờ."}


def _header_key(value: str) -> str:
    text = str(value or "").strip().lower().replace("đ", "d")
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _parse_comment_delay(value: str, unit: str) -> Optional[int]:
    from decimal import Decimal, InvalidOperation

    try:
        raw = (value or "").strip().replace(",", ".")
        amount = Decimal(1) / 6 if unit == "hours" and raw == "1/6" else Decimal(raw)
        exact_minutes = amount * (60 if unit == "hours" else 1)
        if not exact_minutes.is_finite() or exact_minutes < 1 or exact_minutes > 1440:
            return None
        minutes = int(exact_minutes.to_integral_value())
        if abs(exact_minutes - minutes) > Decimal("0.001"):
            return None
        return minutes
    except (InvalidOperation, ValueError, OverflowError):
        return None


def sync_comment_sheet_rows(rows: List[List[str]], legacy_page_id: str = "", save_url: str = "") -> Dict[str, Any]:
    """Apply a multi-Page comment tab while keeping automation permission in the dashboard."""
    from contextlib import closing
    from datetime import datetime, timedelta, timezone

    if not rows:
        return {"success": False, "error": "Tab bình luận không có hàng tiêu đề."}

    header = [_header_key(cell) for cell in rows[0]]
    id_col = next((i for i, key in enumerate(header) if key in ("id trang", "page id", "id page")), None)
    name_col = next((i for i, key in enumerate(header) if key in ("ten fanpage", "page name", "ten trang")), None)
    comment_col = next((i for i, key in enumerate(header) if key in ("comment", "mau binh luan", "noi dung binh luan")), None)
    order_col = next((i for i, key in enumerate(header) if key in ("thu tu", "thu tu comment", "sequence step")), None)
    minute_col = next((i for i, key in enumerate(header) if key in (
        "so phut cho sau khi dang", "so phut cho sau khi dang bai", "delay minutes", "delay phut"
    )), None)
    hour_col = next((i for i, key in enumerate(header) if key in (
        "so gio cho sau khi dang bai", "so gio cho", "delay hours"
    )), None)
    delay_col = minute_col if minute_col is not None else hour_col
    delay_unit = "minutes" if minute_col is not None else "hours"
    if id_col is None or name_col is None or comment_col is None or delay_col is None:
        return {
            "success": False,
            "error": "Tab cần các cột ID trang, Tên Fanpage, Comment và Số phút chờ sau khi đăng.",
        }

    errors: List[str] = []
    warnings: List[str] = []
    grouped: Dict[str, Dict[str, Any]] = {}
    row_page_ids = set()
    pages = db.get_all_pages()
    page_ids: Dict[str, set] = {}
    canonical_names = {str(page["page_id"]): page["page_name"] for page in pages}
    for page in pages:
        canonical_id = str(page["page_id"])
        for identifier in (canonical_id, page.get("fb_profile_id")):
            if identifier:
                page_ids.setdefault(str(identifier), set()).add(canonical_id)

    for row_number, row in enumerate(rows[1:], start=2):
        cells = list(row) + [""] * max(0, len(header) - len(row))
        raw_id = str(cells[id_col] or "").strip().strip("'")
        comment = str(cells[comment_col] or "").strip()
        page_name = str(cells[name_col] or "").strip()
        raw_delay = str(cells[delay_col] or "").strip()
        if not any(str(cell or "").strip() for cell in cells):
            continue
        if not raw_id and not comment and not raw_delay:
            continue
        clean_id, id_error = validate_page_id(raw_id)
        if id_error:
            errors.append(f"Dòng {row_number}: {id_error}")
            continue
        candidates = page_ids.get(clean_id, set())
        if len(candidates) != 1:
            message = (
                f"Dòng {row_number}: ID {clean_id} trùng giữa nhiều Page."
                if candidates else f"Dòng {row_number}: ID {clean_id} chưa có trong danh sách Page."
            )
            errors.append(message)
            for candidate in candidates:
                row_page_ids.add(candidate)
                grouped.setdefault(candidate, {"templates": [], "delays": set(), "errors": [], "names": set(), "steps": {}, "unordered": False})["errors"].append(message)
            continue
        clean_id = next(iter(candidates))
        if delay_unit == "hours" and clean_id != legacy_page_id:
            errors.append(f"Dòng {row_number}: tab tính theo giờ chỉ dùng cho Page pilot cũ.")
            continue
        row_page_ids.add(clean_id)
        group = grouped.setdefault(clean_id, {"templates": [], "delays": set(), "errors": [], "names": set(), "steps": {}, "unordered": False})
        if page_name:
            group["names"].add(page_name)
        if not comment:
            group["errors"].append(f"Dòng {row_number}: thiếu nội dung Comment.")
            continue
        if len(comment) > 8000 or re.search(r"\bEA[A-Z][A-Za-z0-9]{30,}\b", comment):
            group["errors"].append(f"Dòng {row_number}: nội dung không hợp lệ hoặc có vẻ chứa Access Token.")
            continue
        if not validate_spintax_braces(comment):
            group["errors"].append(f"Dòng {row_number}: cú pháp Spintax không hợp lệ.")
            continue
        try:
            comment_content.validate_template(comment, canonical_names[clean_id])
        except ValueError as exc:
            group["errors"].append(f"Dòng {row_number}: {exc}")
            continue
        delay = _parse_comment_delay(raw_delay, delay_unit)
        if delay is None and (raw_delay or delay_unit != "hours"):
            unit_name = "phút" if delay_unit == "minutes" else "giờ"
            group["errors"].append(f"Dòng {row_number}: thời gian chờ {unit_name} phải quy đổi thành 1–1440 phút nguyên.")
            continue
        order = str(cells[order_col] or "").strip() if order_col is not None else ""
        if order:
            if delay_unit != "minutes" or not re.fullmatch(r"[1-9][0-9]{0,3}", order) or delay is None:
                group["errors"].append(f"Dòng {row_number}: Thứ tự phải là số nguyên dương và thời gian chờ phải tính theo phút.")
                continue
            step = int(order)
            if step in group["steps"]:
                group["errors"].append(f"Dòng {row_number}: trùng bình luận thứ {step}.")
                continue
            group["steps"][step] = {"template": comment, "delay_minutes": delay}
        else:
            group["unordered"] = True
        if delay is not None:
            group["delays"].add(delay)
        if comment not in group["templates"]:
            group["templates"].append(comment)

    valid: Dict[str, Dict[str, Any]] = {}
    page_errors: Dict[str, str] = {}
    for page_id, group in grouped.items():
        steps = [group["steps"][key] for key in sorted(group["steps"])]
        if group["errors"]:
            page_errors[page_id] = " ".join(group["errors"])
        elif steps:
            if group["unordered"] or sorted(group["steps"]) != list(range(1, len(steps) + 1)):
                page_errors[page_id] = "Chuỗi bình luận của Page phải đánh số liên tục từ 1 cho tất cả các dòng."
            elif any(a["delay_minutes"] >= b["delay_minutes"] for a, b in zip(steps, steps[1:])):
                page_errors[page_id] = "Số phút chờ phải tăng dần theo thứ tự bình luận."
            else:
                valid[page_id] = {
                    "templates": [step["template"] for step in steps],
                    "delay_minutes": steps[0]["delay_minutes"],
                    "sequence": steps,
                }
        elif len(group["delays"]) != 1:
            page_errors[page_id] = "Các dòng cùng Page phải có cùng một thời gian chờ từ 1 đến 1440 phút."
        elif not group["templates"]:
            page_errors[page_id] = "Page cần ít nhất một mẫu Comment hợp lệ."
        else:
            valid[page_id] = {
                "templates": group["templates"],
                "delay_minutes": next(iter(group["delays"])),
                "sequence": [],
            }
            if len(group["names"]) > 1:
                warnings.append(f"Page {page_id}: tên Fanpage không nhất quán giữa các dòng; dùng Page ID làm khóa.")

    if not valid and save_url:
        detail = errors + [f"Page {pid}: {message}" for pid, message in page_errors.items()]
        return {"success": False, "error": "Không có Page nào có cấu hình hợp lệ để đồng bộ.", "errors": detail}

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    pending_updated = 0
    with closing(db.get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for page_id in set(valid) | set(page_errors):
            message = page_errors.get(page_id)
            if message:
                conn.execute(
                    "UPDATE page_rules SET source_error = ?, source_synced_at = ?, updated_at = ? WHERE page_id = ?",
                    (message, now, now, page_id),
                )
                errors.append(f"Page {page_id}: {message}")
                continue
            item = valid[page_id]
            templates = item["templates"]
            serialized = json.dumps(templates, ensure_ascii=False)
            old = conn.execute("SELECT delay_minutes, comment_sequence FROM page_rules WHERE page_id = ?", (page_id,)).fetchone()
            conn.execute("""
                UPDATE page_rules
                SET delay_minutes = ?, templates = ?, comment_sequence = ?, source_error = NULL,
                    source_synced_at = ?, updated_at = ?
                WHERE page_id = ?
            """, (item["delay_minutes"], serialized, json.dumps(item["sequence"], ensure_ascii=False), now, now, page_id))
            try:
                pending_updated += db.refresh_single_task_content(
                    conn, page_id, old_delay=old["delay_minutes"] if old else None,
                    freeze=bool(item["sequence"] or (old and json.loads(old["comment_sequence"]))),
                )
            except ValueError as exc:
                message = str(exc)
                conn.execute("UPDATE page_rules SET source_error=? WHERE page_id=?", (message, page_id))
                errors.append(f"Page {page_id}: {message}")

        # A missing group blocks that Page until the source contains a valid configuration again.
        for page in pages:
            page_id = str(page["page_id"])
            if page_id not in row_page_ids:
                message = "Page không có mẫu trong tab bình luận hiện tại."
                conn.execute(
                    "UPDATE page_rules SET source_error = ?, source_synced_at = ?, updated_at = ? WHERE page_id = ?",
                    (message, now, now, page_id),
                )
                if page.get("rule_enabled"):
                    errors.append(f"Page {page_id}: {message}")
        if save_url:
            conn.execute("""
                INSERT INTO app_settings (setting_key, setting_value, updated_at)
                VALUES ('comment_sheet_url', ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value,
                                                       updated_at = excluded.updated_at
            """, (save_url, now))

    templates_count = sum(len(item["templates"]) for item in valid.values())
    if not valid:
        return {"success": False, "error": "Không có Page nào có cấu hình hợp lệ để đồng bộ.", "errors": errors}
    return {
        "success": True,
        "pages_synced": len(valid),
        "templates_count": templates_count,
        "pending_updated": pending_updated,
        "errors": errors,
        "warnings": warnings,
        "sheet_mode": "MULTI_PAGE" if minute_col is not None else "LEGACY_HOURS",
        "message": f"Đã đồng bộ {len(valid)} Page, {templates_count} mẫu; cập nhật {pending_updated} tác vụ tự động đang chờ.",
    }


def sync_comment_sheet(sheet_url: str, legacy_page_id: str = "", save_url: bool = False) -> Dict[str, Any]:
    """Reads one configured Google Sheets tab and applies valid per-Page groups."""
    from urllib.parse import urlsplit

    parsed = urlsplit((sheet_url or "").strip())
    match = re.fullmatch(r"/spreadsheets/d/([A-Za-z0-9_-]+)(?:/edit)?/?", parsed.path)
    gid = extract_sheet_gid(sheet_url or "")
    if parsed.scheme != "https" or parsed.netloc != "docs.google.com" or not match or not gid:
        return {"success": False, "error": "Cần URL Google Sheets hợp lệ có gid của tab bình luận."}
    url = f"https://docs.google.com/spreadsheets/d/{match.group(1)}/gviz/tq"
    try:
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            response = client.get(url, params={
                "tqx": "out:csv", "gid": gid, "range": "A1:E2000", "headers": "1",
            })
        if response.status_code != 200 or "text/csv" not in response.headers.get("content-type", ""):
            return {"success": False, "error": "Không đọc được tab bình luận đã cấu hình."}
        rows = list(csv.reader(io.StringIO(response.text)))
    except (httpx.HTTPError, csv.Error):
        return {"success": False, "error": "Lỗi khi đọc tab bình luận từ Google."}
    return sync_comment_sheet_rows(rows, legacy_page_id=legacy_page_id, save_url=sheet_url if save_url else "")
