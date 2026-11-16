"""Persistent per-profile safety gates for browser operations.

State is kept in ``app_settings`` so it survives process restarts without a
schema migration.  This module never reads cookies, credentials, or browser
profile contents.
"""
from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import config
import database as db

_PREFIX = "browser_safety:"
_VALID_BLOCKS = {"READY", "LOGIN_REQUIRED", "CHECKPOINT", "RESTRICTED", "ERROR"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _profile_key(profile_id: str) -> str:
    value = str(profile_id or "").strip()
    if not value or len(value) > 200 or any(ord(ch) < 32 for ch in value):
        raise ValueError("profile_id không hợp lệ")
    return _PREFIX + value


def _limits() -> tuple[int, int]:
    max_ops = int(getattr(config, "BROWSER_MAX_OPERATIONS_PER_PROFILE_PER_DAY", 100))
    min_gap = int(getattr(config, "BROWSER_MIN_GAP_PER_PROFILE_SEC", 60))
    return max(1, max_ops), max(0, min_gap)


def _empty(now: datetime) -> Dict[str, Any]:
    return {"version": 1, "day": db.local_day_key(now), "count": 0,
            "last_reserved_at": None, "browser_status": "READY", "generation": 0, "message": ""}


def _load(raw: str, now: datetime) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1:
            return None
        day = value.get("day")
        count = value.get("count")
        generation = value.get("generation")
        status = value.get("browser_status")
        message = value.get("message", "")
        last = value.get("last_reserved_at")
        if not isinstance(day, str) or datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") != day:
            return None
        if day > db.local_day_key(now):
            # A backwards clock must never reset today's already spent budget.
            return None
        if type(count) is not int or count < 0:
            return None
        if type(generation) is not int or generation < 0 or status not in _VALID_BLOCKS:
            return None
        if not isinstance(message, str):
            return None
        if count > 0 and last is None:
            return None
        if last is not None:
            if not isinstance(last, str):
                return None
            parsed = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            # Validate arithmetic too, so a corrupt extreme date fails closed.
            parsed.astimezone(timezone.utc) + timedelta(seconds=_limits()[1])
        return {"version": 1, "day": day, "count": count,
                "last_reserved_at": last, "browser_status": status,
                "generation": generation, "message": message}
    except (TypeError, ValueError, json.JSONDecodeError, OverflowError):
        return None


def _decision(state: Optional[Dict[str, Any]], now: datetime, *, ready=False,
              reason="READY", retry_at=None, message="") -> Dict[str, Any]:
    if state is None:
        return {"ready": False, "reason": "SAFETY_STATE_INVALID", "retry_at": None,
                "message": "Trạng thái an toàn của profile không hợp lệ; đã chặn thao tác.",
                "browser_status": "ERROR", "generation": None, "count": None,
                "day": None}
    return {"ready": bool(ready), "reason": reason, "retry_at": retry_at,
            "message": message, "browser_status": state["browser_status"],
            "generation": state["generation"], "count": state["count"], "day": state["day"]}


def _write(conn, profile_id: str, state: Dict[str, Any], now: datetime) -> None:
    conn.execute("""INSERT INTO app_settings(setting_key,setting_value,updated_at)
                    VALUES (?,?,?) ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value=excluded.setting_value, updated_at=excluded.updated_at""",
                 (_profile_key(profile_id), json.dumps(state, separators=(",", ":")), now.isoformat()))


def _wait(state: Dict[str, Any], now: datetime, max_ops: int, min_gap: int) -> Optional[tuple]:
    waits = []
    if state["count"] >= max_ops:
        waits.append((datetime.fromisoformat(db.next_daily_reset_utc(now)), "WAIT_PROFILE_DAILY",
                      "Profile đã hết hạn mức thao tác trong ngày."))
    if state["last_reserved_at"]:
        last = datetime.fromisoformat(state["last_reserved_at"].replace("Z", "+00:00"))
        earliest = last.astimezone(timezone.utc) + timedelta(seconds=min_gap)
        if earliest > now:
            waits.append((earliest, "WAIT_PROFILE_GAP", "Profile đang chờ khoảng cách giữa các thao tác."))
    return max(waits, key=lambda item: item[0]) if waits else None


def status(profile_id: str) -> Dict[str, Any]:
    """Return the current gate and counters without reserving an operation."""
    try:
        key = _profile_key(profile_id)
    except ValueError:
        return _decision(None, _now())
    now = _now()
    with closing(db.get_connection()) as conn:
        row = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key=?", (key,)).fetchone()
    state = _load(row[0], now) if row else _empty(now)
    if state is None:
        return _decision(None, now)
    max_ops, min_gap = _limits()
    # Day rollover resets only the daily count; the inter-operation gap remains.
    if state["day"] != db.local_day_key(now):
        state["day"] = db.local_day_key(now)
        state["count"] = 0
    result = _decision(state, now, ready=True)
    result["max_operations_per_day"] = max_ops
    result["min_gap_seconds"] = min_gap
    if state["browser_status"] != "READY":
        result.update(ready=False, reason="PROFILE_BLOCKED",
                      message=state["message"] or "Profile đang bị chặn; cần kiểm tra và khôi phục.")
    else:
        wait = _wait(state, now, max_ops, min_gap)
        if wait:
            deadline, reason, message = wait
            result.update(ready=False, reason=reason, retry_at=deadline.isoformat(), message=message)
    return result


def reserve_operation(profile_id: str, allow_blocked: bool = False) -> Dict[str, Any]:
    """Atomically reserve one browser operation for a profile."""
    try:
        key = _profile_key(profile_id)
    except ValueError:
        return _decision(None, _now())
    now = _now()
    local_day = db.local_day_key(now)
    max_ops, min_gap = _limits()
    with closing(db.get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key=?", (key,)).fetchone()
        state = _load(row[0], now) if row else _empty(now)
        if state is None:
            return _decision(None, now)
        if state["day"] != local_day:
            state["day"], state["count"] = local_day, 0
        if state["browser_status"] == "ERROR" or (state["browser_status"] != "READY" and not allow_blocked):
            return _decision(state, now, reason="PROFILE_BLOCKED",
                             message=state["message"] or "Profile đang bị chặn; cần kiểm tra và khôi phục.")
        wait = _wait(state, now, max_ops, min_gap)
        if wait:
            deadline, reason, message = wait
            return _decision(state, now, reason=reason, retry_at=deadline.isoformat(), message=message)
        state["count"] += 1
        state["last_reserved_at"] = now.isoformat()
        _write(conn, profile_id, state, now)
        result = _decision(state, now, ready=True)
        result["message"] = "Đã giữ lượt thao tác cho profile."
        return result


def block(profile_id: str, status: str, message: str) -> Dict[str, Any]:
    """Block a profile and quarantine its Browser pages/tasks atomically."""
    if status not in {"LOGIN_REQUIRED", "CHECKPOINT", "RESTRICTED"}:
        raise ValueError("Trạng thái chặn profile không hợp lệ")
    key = _profile_key(profile_id)
    profile_id = key[len(_PREFIX):]
    now = _now()
    with closing(db.get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key=?", (key,)).fetchone()
        state = _load(row[0], now) if row else _empty(now)
        if state is not None:
            state["browser_status"] = status
            state["message"] = str(message or "")[:1000]
            state["generation"] += 1
            _write(conn, profile_id, state, now)
        page_rows = conn.execute("SELECT page_id FROM pages WHERE send_mode='BROWSER' AND antidetect_profile_id=?", (profile_id,)).fetchall()
        ids = [row[0] for row in page_rows]
        if ids:
            marks = ",".join("?" for _ in ids)
            conn.execute(f"""UPDATE pages SET browser_status=?,browser_checked_at=NULL,browser_error=?,
                         browser_identity_id=NULL,browser_profile_id=NULL,updated_at=?
                         WHERE page_id IN ({marks}) AND send_mode='BROWSER'""", [status, str(message or "")[:1000], now.isoformat(), *ids])
            conn.execute(f"""UPDATE tasks SET status='PAUSED',error_message=?,updated_at=?
                         WHERE status='PENDING' AND page_id IN ({marks})""", [str(message or "")[:1000], now.isoformat(), *ids])
        return _decision(state, now, reason="PROFILE_BLOCKED", message=str(message or ""))


def clear_block(profile_id: str, expected_generation: int) -> Dict[str, Any]:
    """Clear a block only when the caller holds the current generation token."""
    try:
        key = _profile_key(profile_id)
    except ValueError:
        return {"cleared": False, "reason": "SAFETY_STATE_INVALID"}
    now = _now()
    with closing(db.get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key=?", (key,)).fetchone()
        state = _load(row[0], now) if row else None
        if state is None:
            return {"cleared": False, "reason": "SAFETY_STATE_INVALID"}
        if type(expected_generation) is not int or state["generation"] != expected_generation:
            return {"cleared": False, "reason": "STALE_GENERATION", "generation": state["generation"]}
        if state["browser_status"] == "ERROR":
            return {"cleared": False, "reason": "SAFETY_STATE_INVALID", "generation": state["generation"]}
        state["browser_status"] = "READY"
        state["message"] = ""
        state["generation"] += 1
        _write(conn, profile_id, state, now)
        return {"cleared": True, "reason": "CLEARED", "generation": state["generation"], "browser_status": "READY"}
