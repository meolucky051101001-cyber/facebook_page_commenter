"""Conservative permalink identity for deduplication, never sending authorization."""

import re
from typing import Iterable, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit


_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com"}
_IDENTITY_FIELDS = {"id", "story_fbid", "fbid", "v"}
_TRACKING_FIELDS = {"fbclid", "mibextid", "ref", "refid", "rdid", "__cft__", "__tn__"}


def facebook_post_key(
    url: str, *, allowed_owner_ids: Iterable[str]
) -> Optional[Tuple[str, str]]:
    """Recognize numeric Facebook permalinks within a caller-enforced Page scope.

    A Reel, video, and ordinary post are separate namespaces. Owner-bearing URLs
    require a known numeric Page identity. Unsupported or ambiguous URLs have no
    key, so they cannot cause otherwise distinct schedules to be suppressed.
    """
    if not isinstance(url, str) or not url or "\\" in url:
        return None
    if any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in _HOSTS
                or parsed.netloc.lower() not in {parsed.hostname, f"{parsed.hostname}:443"}
                or parsed.fragment):
            return None
        pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=100)
    except ValueError:
        return None

    identity = {}
    for name, value in pairs:
        if name in {"comment_id", "reply_comment_id"}:
            return None
        if name in _IDENTITY_FIELDS:
            if name in identity or not re.fullmatch(r"[0-9]+", value):
                return None
            identity[name] = value
        elif name not in _TRACKING_FIELDS and not re.fullmatch(r"__cft__\[[0-9]+\]", name):
            return None

    owners = {str(owner) for owner in allowed_owner_ids
              if re.fullmatch(r"[0-9]+", str(owner))}
    if parsed.path == "/permalink.php":
        if set(identity) == {"story_fbid", "id"} and identity["id"] in owners:
            return "post", identity["story_fbid"]
        return None
    if identity:
        return None

    match = re.fullmatch(r"/reel/([0-9]+)/?", parsed.path)
    if match:
        return "reel", match[1]
    match = re.fullmatch(r"/videos/([0-9]+)/?", parsed.path)
    if match:
        return "video", match[1]
    match = re.fullmatch(r"/([0-9]+)/(posts|videos)/([0-9]+)/?", parsed.path)
    if match and match[1] in owners:
        return ("post" if match[2] == "posts" else "video"), match[3]
    return None
