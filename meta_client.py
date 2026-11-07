import re
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple
import httpx
from dateutil import parser as date_parser

import config

GRAPH_API_BASE = f"https://graph.facebook.com/{config.GRAPH_API_VERSION}"
REQUIRED_SCOPES = {"pages_show_list", "pages_read_engagement", "pages_manage_engagement"}

class MetaAPIError(Exception):
    def __init__(self, code: int, subcode: Optional[int], message: str, category: str):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.message = message
        self.category = category  # 'TOKEN_EXPIRED', 'POST_ERROR', 'RATE_LIMIT', 'NEED_CHECK', 'UNKNOWN'

def classify_error(err_data: Dict[str, Any]) -> Tuple[str, str]:
    code = err_data.get("code", 0)
    subcode = err_data.get("error_subcode", 0)
    msg = err_data.get("message", "Unknown Meta API error")

    if code in [190, 102] or subcode in [458, 460, 463, 467] or "access token" in msg.lower():
        return "TOKEN_EXPIRED", f"Token hết hạn hoặc bị thu hồi (Mã {code}/{subcode}): {msg}"
    elif code in [4, 17, 32, 613] or "rate limit" in msg.lower() or "too many calls" in msg.lower():
        return "RATE_LIMIT", f"Chạm giới hạn tần suất Meta (Mã {code}/{subcode}): {msg}"
    elif code in [100, 200] and ("does not exist" in msg.lower() or "permission" in msg.lower() or "not visible" in msg.lower()):
        return "POST_ERROR", f"Bài viết không tồn tại hoặc không thể bình luận (Mã {code}/{subcode}): {msg}"
    return "UNKNOWN", f"Lỗi Meta (Mã {code}/{subcode}): {msg}"

def exchange_for_long_lived_user_token(short_lived_token: str) -> Tuple[str, Optional[int]]:
    """
    Exchanges short-lived user token for a ~60 day long-lived user token.
    If Meta App credentials are not configured, returns original token.
    """
    if not config.META_APP_ID or not config.META_APP_SECRET:
        return short_lived_token, None

    url = f"{GRAPH_API_BASE}/oauth/access_token"
    params = {
        "grant_type": "fb_exchange_token",
        "client_id": config.META_APP_ID,
        "client_secret": config.META_APP_SECRET,
        "fb_exchange_token": short_lived_token
    }
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(url, params=params)
        data = resp.json()
        if "error" in data:
            category, msg = classify_error(data["error"])
            raise MetaAPIError(data["error"].get("code", 0), data["error"].get("error_subcode"), msg, category)
        long_lived_token = data.get("access_token", short_lived_token)
        expires_in = data.get("expires_in")
        return long_lived_token, expires_in

def debug_token(token: str) -> Dict[str, Any]:
    """
    Inspects token scopes, validity, and expiration using debug_token endpoint.
    """
    app_access_token = f"{config.META_APP_ID}|{config.META_APP_SECRET}" if (config.META_APP_ID and config.META_APP_SECRET) else token
    url = f"{GRAPH_API_BASE}/debug_token"
    params = {
        "input_token": token,
        "access_token": app_access_token
    }
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(url, params=params)
        data = resp.json()
        if "error" in data:
            return {"is_valid": False, "status": "EXPIRED", "missing_scopes": list(REQUIRED_SCOPES), "expires_at": None}

        info = data.get("data", {})
        is_valid = info.get("is_valid", False)
        scopes = set(info.get("scopes", []))
        expires_at_timestamp = info.get("expires_at", 0)

        missing = list(REQUIRED_SCOPES - scopes)
        expires_iso = None
        if expires_at_timestamp > 0:
            expires_iso = datetime.fromtimestamp(expires_at_timestamp, tz=timezone.utc).isoformat()

        status = "OK"
        if not is_valid:
            status = "EXPIRED"
        elif missing:
            status = "MISSING_PERMISSIONS"
        elif expires_at_timestamp > 0 and (expires_at_timestamp - datetime.now(timezone.utc).timestamp()) < 7 * 86400:
            status = "EXPIRING_SOON"

        return {
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
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(url, params=params)
        data = resp.json()
        if "error" in data:
            category, msg = classify_error(data["error"])
            raise MetaAPIError(data["error"].get("code", 0), data["error"].get("error_subcode"), msg, category)
        return data

def get_user_pages(user_access_token: str) -> List[Dict[str, Any]]:
    """
    Fetches all managed pages and their respective Page Access Tokens using user token.
    """
    url = f"{GRAPH_API_BASE}/me/accounts"
    params = {
        "access_token": user_access_token,
        "fields": "id,name,access_token,category,tasks"
    }
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(url, params=params)
        data = resp.json()
        if "error" in data:
            category, formatted_msg = classify_error(data["error"])
            raise MetaAPIError(data["error"].get("code", 0), data["error"].get("error_subcode"), formatted_msg, category)
        return data.get("data", [])

def verify_post_belongs_to_page(post_id_or_url: str, page_id: str, page_access_token: str) -> Dict[str, Any]:
    """
    Verifies that a post exists and was published by the specified page.
    Extracts post_id if a full Facebook URL was provided.
    """
    clean_post_id = post_id_or_url.strip()
    # Check if URL was pasted
    url_match = re.search(r'posts/([0-9]+)', clean_post_id) or re.search(r'story_fbid=([0-9]+)', clean_post_id)
    if url_match:
        extracted = url_match.group(1)
        # Often Facebook post IDs for graph API are formatted as {page_id}_{post_id}
        if "_" not in extracted:
            clean_post_id = f"{page_id}_{extracted}"
        else:
            clean_post_id = extracted

    url = f"{GRAPH_API_BASE}/{clean_post_id}"
    params = {
        "access_token": page_access_token,
        "fields": "id,from,created_time,message,permalink_url"
    }
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(url, params=params)
        data = resp.json()
        if "error" in data:
            category, formatted_msg = classify_error(data["error"])
            raise MetaAPIError(data["error"].get("code", 0), data["error"].get("error_subcode"), formatted_msg, category)

        from_id = data.get("from", {}).get("id")
        if str(from_id) != str(page_id):
            raise MetaAPIError(
                code=100,
                subcode=0,
                message=f"Bài viết không thuộc về Page này (Người đăng: {from_id}, Page ID: {page_id}).",
                category="POST_ERROR"
            )

        data["clean_post_id"] = clean_post_id
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
        params["since"] = since_timestamp

    with httpx.Client(timeout=15.0) as client:
        resp = client.get(url, params=params)
        data = resp.json()
        if "error" in data:
            category, formatted_msg = classify_error(data["error"])
            raise MetaAPIError(data["error"].get("code", 0), data["error"].get("error_subcode"), formatted_msg, category)
        return data.get("data", [])

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
        with httpx.Client(timeout=15.0) as client:
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
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(url, data=data)

            # Check rate limit headers
            rate_limit_header = resp.headers.get("x-business-use-case-usage") or resp.headers.get("x-app-usage")

            result = resp.json()
            if "error" in result:
                category, formatted_msg = classify_error(result["error"])
                return {
                    "success": False,
                    "category": category,
                    "error": formatted_msg,
                    "rate_limit_header": rate_limit_header
                }
            return {
                "success": True,
                "comment_id": result.get("id"),
                "rate_limit_header": rate_limit_header
            }

    except httpx.ConnectError as e:
        # Network failed BEFORE request reached Meta -> safe to retry
        return {
            "success": False,
            "category": "NETWORK_RETRYABLE",
            "error": f"Không thể kết nối đến máy chủ Meta: {str(e)}"
        }
    except (httpx.TimeoutException, httpx.ReadTimeout):
        # Request was sent but response timed out -> Meta might have posted it!
        # Try self-reconciliation
        existing_comment_id = check_comment_exists(post_id, page_id, page_access_token, message)
        if existing_comment_id:
            return {
                "success": True,
                "comment_id": existing_comment_id,
                "note": "Tự động đối chiếu thành công sau khi mạng timeout."
            }
        return {
            "success": False,
            "category": "NEED_CHECK",
            "error": "Quá thời gian chờ phản hồi mạng. Meta có thể đã nhận lệnh đăng, vui lòng đối chiếu trên Facebook."
        }
    except Exception as e:
        return {
            "success": False,
            "category": "NEED_CHECK",
            "error": f"Lỗi không xác định khi gửi: {str(e)}"
        }
