import re
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit
from typing import Dict, Any, List, Optional, Tuple
import httpx
from dateutil import parser as date_parser

import config
import crypto

GRAPH_API_BASE = f"https://graph.facebook.com/{config.GRAPH_API_VERSION}"
REQUIRED_SCOPES = {"pages_show_list", "pages_read_engagement", "pages_manage_engagement"}


# httpx 0.28 eagerly parses OS proxy settings, which crash the client when Windows
# has a malformed registry proxy. Meta Graph calls must not inherit that proxy.
def _http_client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, trust_env=False)


class MetaAPIError(Exception):
    def __init__(self, code: int, subcode: Optional[int], message: str, category: str, retry_after_seconds=None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.message = message
        self.category = category  # 'TOKEN_EXPIRED', 'POST_ERROR', 'RATE_LIMIT', 'NEED_CHECK', 'UNKNOWN'
        self.retry_after_seconds = retry_after_seconds

    def as_result(self):
        return {"success": False, "category": self.category, "error": self.message,
                "rate_limit_scope": rate_limit_scope(self.code), "retry_after_seconds": self.retry_after_seconds}


def rate_limit_scope(code):
    return "APP" if code == 4 else ("ACCOUNT" if code == 17 else "PAGE")


def retry_after_seconds(headers):
    from email.utils import parsedate_to_datetime
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw) if str(raw).strip().isdigit() else (
            parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
        return max(0, seconds)
    except (TypeError, ValueError, OverflowError):
        return None


def sanitize_error_message(msg: str) -> str:
    """Masks any raw EAAB... access tokens that might appear in exception strings or URLs."""
    if not msg:
        return ""
    return re.sub(r'EAA[a-zA-Z0-9]{10,}', lambda m: crypto.mask_token(m.group(0)), msg)


def classify_error(err_data: Dict[str, Any]) -> Tuple[str, str]:
    code = err_data.get("code", 0)
    subcode = err_data.get("error_subcode", 0)
    msg = sanitize_error_message(err_data.get("message", "Unknown Meta API error"))

    if code in [190, 102] or subcode in [458, 460, 463, 467] or "access token" in msg.lower():
        return "TOKEN_EXPIRED", f"Token hết hạn hoặc bị thu hồi (Mã {code}/{subcode}): {msg}"
    elif code in [4, 17, 32, 613] or "rate limit" in msg.lower() or "too many calls" in msg.lower():
        return "RATE_LIMIT", f"Chạm giới hạn tần suất Meta (Mã {code}/{subcode}): {msg}"
    elif code in (10, 200, 294) and "permission" in msg.lower():
        return "MISSING_PERMISSIONS", f"Meta chưa cấp quyền cần thiết (Mã {code}/{subcode})."
    elif code in [100, 200] and (
        "does not exist" in msg.lower()
        or "permission" in msg.lower()
        or "not visible" in msg.lower()
        or "unsupported get request" in msg.lower()
    ):
        return "POST_ERROR", f"Bài viết không tồn tại hoặc không thể bình luận (Mã {code}/{subcode}): {msg}"
    return "UNKNOWN", f"Lỗi Meta (Mã {code}/{subcode}): {msg}"


def parse_usage_header(headers: Any) -> float:
    """
    Extracts maximum API usage percentage (0-100) from X-Business-Use-Case-Usage or X-App-Usage headers.
    """
    if not headers:
        return 0.0
    max_pct = 0.0
    try:
        app_usage = headers.get("x-app-usage")
        if app_usage:
            data = json.loads(app_usage)
            for k in ("call_count", "total_cputime", "total_time"):
                val = float(data.get(k, 0))
                if val > max_pct:
                    max_pct = val

        biz_usage = headers.get("x-business-use-case-usage")
        if biz_usage:
            data = json.loads(biz_usage)
            for _, entries in data.items():
                if isinstance(entries, list):
                    for entry in entries:
                        for k in ("call_count", "total_cputime", "total_time"):
                            val = float(entry.get(k, 0))
                            if val > max_pct:
                                max_pct = val
    except Exception:
        pass
    return max_pct


def exchange_for_long_lived_user_token(short_lived_token: str) -> Tuple[str, Optional[int]]:
    """
    Exchanges short-lived user token for a ~60 day long-lived user token.
    If Meta App credentials are not configured, returns (short_lived_token, None).
    Raises MetaAPIError if credentials ARE configured and Meta rejects the exchange.
    """
    if not config.has_app_credentials():
        return short_lived_token, None

    url = f"{GRAPH_API_BASE}/oauth/access_token"
    params = {
        "grant_type": "fb_exchange_token",
        "client_id": config.META_APP_ID,
        "client_secret": config.META_APP_SECRET,
        "fb_exchange_token": short_lived_token
    }
    with _http_client(15.0) as client:
        resp = client.get(url, params=params)
        data = resp.json()
        if "error" in data:
            category, msg = classify_error(data["error"])
            raise MetaAPIError(data["error"].get("code", 0), data["error"].get("error_subcode"), msg, category)
        long_lived_token = data.get("access_token", short_lived_token)
        expires_in = data.get("expires_in")
        return long_lived_token, expires_in


def exchange_facebook_login_code(code: str, redirect_uri: str) -> str:
    with _http_client(15.0) as client:
        response = client.get(f"{GRAPH_API_BASE}/oauth/access_token", params={
            "client_id": config.META_APP_ID,
            "client_secret": config.META_APP_SECRET,
            "redirect_uri": redirect_uri,
            "code": code,
        })
        payload = response.json()
        if not response.is_success or "error" in payload or not payload.get("access_token"):
            raise MetaAPIError(190, None, "Meta không chấp nhận mã đăng nhập.", "TOKEN_EXPIRED")
        return payload["access_token"]


def debug_token(token: str) -> Dict[str, Any]:
    """
    Inspects token scopes, validity, and expiration using debug_token endpoint.
    Never marks a token as EXPIRED if the debug_token request itself fails due to
    missing app credentials, rate limit, or network error (`checked=False`).
    """
    app_access_token = (
        f"{config.META_APP_ID}|{config.META_APP_SECRET}"
        if config.has_app_credentials()
        else token
    )
    url = f"{GRAPH_API_BASE}/debug_token"
    params = {
        "input_token": token,
        "access_token": app_access_token
    }
    try:
        with _http_client(15.0) as client:
            resp = client.get(url, params=params)
            data = resp.json()
    except Exception as e:
        return {
            "checked": False,
            "is_valid": None,
            "status": "UNKNOWN",
            "scopes": [],
            "missing_scopes": [],
            "expires_at": None,
            "error": sanitize_error_message(str(e))
        }

    if "error" in data or "data" not in data:
        err_msg = sanitize_error_message(data.get("error", {}).get("message", "Không thể kiểm tra debug_token"))
        return {
            "checked": False,
            "is_valid": None,
            "status": "UNKNOWN",
            "scopes": [],
            "missing_scopes": [],
            "expires_at": None,
            "error": err_msg
        }

    info = data.get("data", {})
    is_valid = info.get("is_valid", False)
    raw_scopes = info.get("scopes")
    scopes = set(raw_scopes) if isinstance(raw_scopes, list) else set()
    expires_at_timestamp = info.get("expires_at", 0) or 0

    missing = list(REQUIRED_SCOPES - scopes) if scopes else sorted(REQUIRED_SCOPES)
    expires_iso = None
    if expires_at_timestamp > 0:
        expires_iso = datetime.fromtimestamp(expires_at_timestamp, tz=timezone.utc).isoformat()

    status = "OK"
    if not is_valid:
        status = "EXPIRED"
        missing = list(REQUIRED_SCOPES)
    elif missing:
        status = "MISSING_PERMISSIONS"
    elif expires_at_timestamp > 0 and (expires_at_timestamp - datetime.now(timezone.utc).timestamp()) < 7 * 86400:
        status = "EXPIRING_SOON"

    return {
        "checked": True,
        "is_valid": is_valid,
        "status": status,
        "scopes": list(scopes),
        "missing_scopes": missing,
        "expires_at": expires_iso
    }


def get_user_info(user_token: str) -> Dict[str, Any]:
    """Fetches user ID and Name for the current user token."""
    url = f"{GRAPH_API_BASE}/me"
    params = {"access_token": user_token, "fields": "id,name"}
    with _http_client(15.0) as client:
        resp = client.get(url, params=params)
        data = resp.json()
        if "error" in data:
            category, msg = classify_error(data["error"])
            raise MetaAPIError(data["error"].get("code", 0), data["error"].get("error_subcode"), msg, category)
        return data


def get_page_identity(page_access_token: str) -> Dict[str, Any]:
    """Returns the identity represented by a Page access token."""
    url = f"{GRAPH_API_BASE}/me"
    params = {"access_token": page_access_token, "fields": "id,name"}
    with _http_client(15.0) as client:
        resp = client.get(url, params=params)
        data = resp.json()
        if "error" in data:
            category, message = classify_error(data["error"])
            raise MetaAPIError(data["error"].get("code", 0), data["error"].get("error_subcode"), message, category)
        return data


def verify_page_connection(page_id: str, page_access_token: str) -> Dict[str, Any]:
    """Read-only verification of token permissions, identity, and Page post access."""
    token_info = debug_token(page_access_token)
    if token_info.get("checked") is not True:
        return {
            "success": False,
            "permanent": False,
            "error": token_info.get("error") or "Không xác minh được quyền của Page token.",
            "token_info": token_info,
        }
    if not token_info.get("is_valid"):
        return {
            "success": False,
            "failure_status": "EXPIRED",
            "error": "Page token đã hết hạn hoặc bị thu hồi.",
            "token_info": token_info,
        }
    missing = token_info.get("missing_scopes") or []
    if missing:
        return {
            "success": False,
            "failure_status": "MISSING_PERMISSIONS",
            "error": "Thiếu quyền: " + ", ".join(missing),
            "token_info": token_info,
        }

    try:
        identity = get_page_identity(page_access_token)
    except MetaAPIError as exc:
        return {"success": False, "failure_status": {"TOKEN_EXPIRED": "EXPIRED", "MISSING_PERMISSIONS": "MISSING_PERMISSIONS"}.get(exc.category),
                "error": exc.message, "token_info": token_info}
    except Exception:
        return {"success": False, "permanent": False,
                "error": "Không thể xác minh danh tính Page token.", "token_info": token_info}
    if str(identity.get("id") or "") != str(page_id):
        return {
            "success": False,
            "failure_status": "IDENTITY_MISMATCH",
            "error": "Page token thuộc Page ID khác với Page đang chọn.",
            "token_info": token_info,
            "identity": identity,
        }

    try:
        posts = get_recent_posts(page_id, page_access_token, limit=1)
    except MetaAPIError as exc:
        return {"success": False, "failure_status": {"TOKEN_EXPIRED": "EXPIRED", "MISSING_PERMISSIONS": "MISSING_PERMISSIONS"}.get(exc.category),
                "error": exc.message, "token_info": token_info, "identity": identity}
    except Exception:
        return {"success": False, "permanent": False,
                "error": "Không thể đọc bài viết của Page.", "token_info": token_info, "identity": identity}
    return {
        "success": True,
        "permanent": False,
        "token_info": token_info,
        "identity": identity,
        "posts_read": len(posts),
    }


def get_user_pages(user_access_token: str) -> List[Dict[str, Any]]:
    """
    Fetches all managed pages and their respective Page Access Tokens using user token.
    Supports pagination if user manages more than 25 pages.
    """
    url = f"{GRAPH_API_BASE}/me/accounts"
    params = {
        "access_token": user_access_token,
        "fields": "id,name,access_token,category,tasks",
        "limit": 100
    }
    pages = []
    seen = set()
    with _http_client(15.0) as client:
        while True:
            resp = client.get(url, params=params)
            data = resp.json()
            if "error" in data:
                category, message = classify_error(data["error"])
                raise MetaAPIError(data["error"].get("code", 0), data["error"].get("error_subcode"), message, category)
            resp.raise_for_status()
            pages.extend(data.get("data", []))
            paging = data.get("paging", {})
            if not paging.get("next"):
                return pages
            cursor = paging.get("cursors", {}).get("after")
            if not cursor or cursor in seen or len(seen) >= 100:
                raise MetaAPIError(100, None, "Meta chưa trả đầy đủ danh sách Page.", "POST_ERROR")
            seen.add(cursor)
            params["after"] = cursor


def extract_post_id_from_input(post_id_or_url: str, page_id: str) -> str:
    """
    Normalizes a user-supplied post ID or Facebook URL into a Graph API post identifier.
    Supports:
      - {page_id}_{post_id}
      - numeric post_id -> {page_id}_{post_id}
      - URLs containing /posts/{id}, story_fbid={id}, /videos/{id}, /reel/{id}, pfbid...
    """
    raw = post_id_or_url.strip()
    if "://" in raw:
        url = urlsplit(raw)
        if url.scheme != "https" or url.hostname not in {"facebook.com", "www.facebook.com", "m.facebook.com"}:
            raise MetaAPIError(100, None, "Chỉ hỗ trợ URL bài viết HTTPS của Facebook.", "POST_ERROR")
        raw = url.path + ("?" + url.query if url.query else "")
    patterns = [
        r'story_fbid=([a-zA-Z0-9_]+)',
        r'/posts/([a-zA-Z0-9_]+)',
        r'/videos/([0-9]+)',
        r'/reel/([0-9]+)',
        r'/photos/[^/]+/([0-9]+)',
    ]
    extracted = None
    for pat in patterns:
        m = re.search(pat, raw)
        if m:
            extracted = m.group(1)
            break

    candidate = extracted if extracted else raw
    # Strip trailing slashes or query params if still URL-like
    candidate = candidate.split("?")[0].strip("/")

    if re.match(r'^[0-9]+$', candidate):
        return f"{page_id}_{candidate}"
    if candidate.startswith("pfbid") and "_" not in candidate:
        return f"{page_id}_{candidate}"
    if not re.fullmatch(r"[A-Za-z0-9_]+", candidate):
        raise MetaAPIError(100, None, "ID bài viết không hợp lệ.", "POST_ERROR")
    return candidate


def verify_post_belongs_to_page(post_id_or_url: str, page_id: str, page_access_token: str) -> Dict[str, Any]:
    """
    Verifies that a post exists and was published by the specified page.
    Extracts post_id if a full Facebook URL was provided.
    """
    clean_post_id = extract_post_id_from_input(post_id_or_url, page_id)

    url = f"{GRAPH_API_BASE}/{clean_post_id}"
    params = {
        "access_token": page_access_token,
        "fields": "id,from,created_time,message,permalink_url"
    }
    with _http_client(15.0) as client:
        resp = client.get(url, params=params)
        data = resp.json()
        if "error" in data:
            category, formatted_msg = classify_error(data["error"])
            raise MetaAPIError(data["error"].get("code", 0), data["error"].get("error_subcode"), formatted_msg, category,
                               retry_after_seconds(resp.headers))

        from_id = data.get("from", {}).get("id")
        if str(from_id) != str(page_id):
            raise MetaAPIError(
                code=100,
                subcode=0,
                message=f"Bài viết không thuộc về Page này (Người đăng: {from_id}, Page ID: {page_id}).",
                category="POST_ERROR"
            )

        data["clean_post_id"] = data.get("id") or clean_post_id
        return data


def get_recent_posts(page_id: str, page_access_token: str, since_timestamp: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Fetches recent published posts for a given page, supporting incremental fetching with 'since'.
    """
    url = f"{GRAPH_API_BASE}/{page_id}/published_posts"
    params = {
        "access_token": page_access_token,
        "fields": "id,message,created_time,permalink_url",
        "limit": limit
    }
    if since_timestamp:
        params["since"] = int(date_parser.isoparse(since_timestamp).timestamp())

    posts = []
    seen_cursors = set()
    with _http_client(15.0) as client:
        while True:
            resp = client.get(url, params=params)
            data = resp.json()
            if "error" in data:
                category, msg = classify_error(data["error"])
                raise MetaAPIError(data["error"].get("code", 0), data["error"].get("error_subcode"), msg, category)
            resp.raise_for_status()
            posts.extend(data["data"])
            paging = data.get("paging", {})
            if not since_timestamp or not paging.get("next"):
                return posts
            cursor = paging.get("cursors", {}).get("after")
            if not cursor or cursor in seen_cursors or len(seen_cursors) >= 100:
                raise MetaAPIError(100, None, "Không đọc hết danh sách bài; giữ nguyên mốc quét.", "POST_ERROR")
            seen_cursors.add(cursor)
            params["after"] = cursor


def page_request(method: str, endpoint: str, token: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Make a request for a Page using its authorized token."""
    with _http_client(25.0) as client:
        headers = {"Authorization": f"Bearer {token}"}
        options = {"params" if method == "GET" else "data": fields}
        resp = client.request(method, f"{GRAPH_API_BASE}/{endpoint}", headers=headers, **options)
        data = resp.json()
        if "error" in data:
            error = data["error"]
            category, message = classify_error(error)
            raise MetaAPIError(error.get("code", 0), error.get("error_subcode"), message, category,
                               retry_after_seconds(resp.headers))
        resp.raise_for_status()
        return data


def publish_page_post(page_id: str, token: str, media_type: str, media_url: str, caption: str) -> str:
    if media_type == "text":
        return str(page_request("POST", f"{page_id}/feed", token, {"message": caption})["id"])
    if media_type == "photo":
        data = page_request("POST", f"{page_id}/photos", token, {"url": media_url, "message": caption})
        return str(data["post_id"])

    reel = page_request("POST", "me/video_reels", token, {"upload_phase": "start"})
    video_id = str(reel["video_id"])
    if not video_id.isdigit():
        raise ValueError("Meta trả ID video không hợp lệ.")
    # The upload host is fixed and the source URL is passed to Meta for fetching.
    with _http_client(90.0) as client:
        resp = client.post(
            f"https://rupload.facebook.com/video-upload/{config.GRAPH_API_VERSION}/{video_id}",
            headers={"Authorization": f"OAuth {token}", "file_url": media_url},
        )
        resp.raise_for_status()
        if resp.json().get("success") is not True:
            raise ValueError("Meta chưa xác nhận tải Reel.")
    result = page_request("POST", "me/video_reels", token, {
        "upload_phase": "finish", "video_state": "PUBLISHED", "video_id": video_id, "description": caption,
    })
    if result.get("success") is not True:
        raise ValueError("Meta chưa xác nhận xuất bản Reel.")
    # Processing is asynchronous. The worker waits for the published object before queuing comments.
    return video_id


def check_comment_exists(post_id: str, page_id: str, page_access_token: str, message: str, within_minutes: int = 15) -> Optional[str]:
    """
    Checks if a comment with similar text was recently posted by the page on the post.
    Used for self-reconciliation when network timed out.
    """
    url = f"{GRAPH_API_BASE}/{post_id}/comments"
    params = {
        "access_token": page_access_token,
        "filter": "stream",
        "limit": 25,
        "fields": "id,from,message,created_time"
    }
    try:
        with _http_client(15.0) as client:
            resp = client.get(url, params=params)
            data = resp.json()
            comments = data.get("data", [])
            now_utc = datetime.now(timezone.utc)
            for c in comments:
                from_id = c.get("from", {}).get("id")
                c_msg = c.get("message", "")
                created_str = c.get("created_time")
                if str(from_id) == str(page_id) and c_msg.strip() == message.strip():
                    if created_str:
                        dt = date_parser.parse(created_str).astimezone(timezone.utc)
                        if (now_utc - dt).total_seconds() <= within_minutes * 60:
                            return c.get("id")
                    else:
                        return c.get("id")
    except Exception:
        pass
    return None


def post_comment(post_id: str, page_id: str, page_access_token: str, message: str) -> Dict[str, Any]:
    """
    Posts a comment to a specific post under the Page's identity.
    Safely differentiates network errors before transmission vs timeout after submission.
    """
    url = f"{GRAPH_API_BASE}/{post_id}/comments"
    data = {
        "access_token": page_access_token,
        "message": message
    }

    try:
        with _http_client(20.0) as client:
            resp = client.post(url, data=data)

            usage_pct = parse_usage_header(resp.headers)
            rate_limit_header = resp.headers.get("x-business-use-case-usage") or resp.headers.get("x-app-usage")

            result = resp.json()
            if "error" in result:
                category, formatted_msg = classify_error(result["error"])
                return {
                    "success": False,
                    "category": category,
                    "error": formatted_msg,
                    "rate_limit_header": rate_limit_header,
                    "usage_pct": usage_pct,
                    "rate_limit_scope": rate_limit_scope(result["error"].get("code")),
                    "retry_after_seconds": retry_after_seconds(resp.headers),
                }
            if not 200 <= resp.status_code < 300 or not result.get("id"):
                return {
                    "success": False,
                    "category": "NEED_CHECK",
                    "error": "Meta chưa trả ID bình luận; cần đối chiếu bài trước khi gửi lại.",
                }
            return {
                "success": True,
                "comment_id": result["id"],
                "rate_limit_header": rate_limit_header,
                "usage_pct": usage_pct
            }

    except httpx.ConnectError as e:
        # Network failed BEFORE request reached Meta -> safe to retry
        return {
            "success": False,
            "category": "NETWORK_RETRYABLE",
            "error": f"Không thể kết nối đến máy chủ Meta: {sanitize_error_message(str(e))}"
        }
    except (httpx.TimeoutException, httpx.ReadTimeout):
        # Request was sent but response timed out -> Meta might have posted it!
        return {
            "success": False,
            "category": "NEED_CHECK",
            "error": "Quá thời gian chờ phản hồi mạng. Meta có thể đã nhận lệnh đăng, vui lòng đối chiếu trên Facebook."
        }
    except Exception as e:
        return {
            "success": False,
            "category": "NEED_CHECK",
            "error": f"Lỗi không xác định khi gửi: {sanitize_error_message(str(e))}"
        }
