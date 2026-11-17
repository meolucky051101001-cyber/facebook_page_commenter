"""Persistent per-profile safety gates for browser operations.

State is kept in ``app_settings`` so it survives process restarts without a
schema migration.  This module never reads cookies, credentials, or browser
profile contents.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import closing, nullcontext
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import config
import database as db
import submit_control

_PREFIX = "browser_safety:"
_VALID_BLOCKS = {"READY", "LOGIN_REQUIRED", "CHECKPOINT", "RESTRICTED", "RECOVERY_REQUIRED", "ERROR"}
_FACEBOOK_BLOCKS = {"LOGIN_REQUIRED", "CHECKPOINT", "RESTRICTED"}
_INSTANCE_ID = uuid.uuid4().hex
_MEMORY_LOCK = threading.Lock()
_EMERGENCY_BLOCKS = {}
_ACTIVE_OPERATIONS = {}
_LEGACY_CHECKS = {}


def _scope(profile_id):
    # Tests and separate repositories must not share an emergency block.
    return (os.path.normcase(os.path.abspath(os.fspath(db.DB_PATH))), profile_id)


def latch(profile_id: str, status: str, message: str) -> str:
    """Stop a profile in this process before attempting any persistent write."""
    profile_id = _profile_key(profile_id)[len(_PREFIX):]
    if status not in _VALID_BLOCKS - {"READY"}:
        raise ValueError("Trạng thái chặn profile không hợp lệ")
    token = uuid.uuid4().hex
    # Publish the in-memory stop while profile admission is suspended. There is
    # no DB, metadata or Browser work under these short control sections.
    with submit_control.profile_mutation(profile_id):
        with _MEMORY_LOCK:
            existing = _EMERGENCY_BLOCKS.get(_scope(profile_id))
            if (existing and existing["browser_status"] in _FACEBOOK_BLOCKS
                    and status not in _FACEBOOK_BLOCKS):
                return existing["token"]
            _EMERGENCY_BLOCKS[_scope(profile_id)] = {
                "token": token, "browser_status": status, "message": str(message or "")[:1000]}
    return token


def _emergency(profile_id, state=None):
    with _MEMORY_LOCK:
        value = _EMERGENCY_BLOCKS.get(_scope(profile_id))
        value = dict(value) if value else None
    if (value and state and state["browser_status"] in _FACEBOOK_BLOCKS
            and value["browser_status"] not in _FACEBOOK_BLOCKS):
        value.update(browser_status=state["browser_status"], message=state["message"])
    return value


def _unlatch(profile_id, token):
    with _MEMORY_LOCK:
        value = _EMERGENCY_BLOCKS.get(_scope(profile_id))
        if value and value["token"] == token:
            del _EMERGENCY_BLOCKS[_scope(profile_id)]


def _activate(profile_id, operation_id):
    with _MEMORY_LOCK:
        _ACTIVE_OPERATIONS[_scope(profile_id)] = operation_id
        _LEGACY_CHECKS.pop(_scope(profile_id), None)


def _deactivate(profile_id, operation_id):
    with _MEMORY_LOCK:
        if _ACTIVE_OPERATIONS.get(_scope(profile_id)) == operation_id:
            del _ACTIVE_OPERATIONS[_scope(profile_id)]


def _is_active(profile_id, operation):
    if not operation or operation["owner_instance_id"] != _INSTANCE_ID:
        return False
    with _MEMORY_LOCK:
        return _ACTIVE_OPERATIONS.get(_scope(profile_id)) == operation["operation_id"]


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
    return {"version": 2, "day": db.local_day_key(now), "count": 0,
            "last_reserved_at": None, "browser_status": "READY", "generation": 0,
            "message": "", "operation": None}


def _valid_operation(value, generation):
    if not isinstance(value, dict):
        return False
    for field in ("operation_id", "owner_instance_id"):
        text = value.get(field)
        if not isinstance(text, str) or len(text) != 32 or any(c not in "0123456789abcdef" for c in text):
            return False
    page_id = value.get("page_id")
    if not isinstance(page_id, str) or (page_id and (not page_id.isascii() or not page_id.isdigit())):
        return False
    if value.get("kind") not in {"check", "scan", "verify", "send"}:
        return False
    if type(value.get("generation")) is not int or value["generation"] != generation:
        return False
    task_id = value.get("task_id")
    if task_id is not None and (type(task_id) is not int or task_id <= 0):
        return False
    started = value.get("started_at")
    if not isinstance(started, str):
        return False
    parsed = datetime.fromisoformat(started.replace("Z", "+00:00"))
    return parsed.tzinfo is not None


def _load(raw: str, now: datetime) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") not in (1, 2):
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
        operation = value.get("operation")
        if value["version"] == 2 and "operation" not in value:
            return None
        if value["version"] == 1 and operation is not None:
            return None
        if operation is not None and not _valid_operation(operation, generation):
            return None
        return {"version": 2, "day": day, "count": count,
                "last_reserved_at": last, "browser_status": status,
                "generation": generation, "message": message, "operation": operation}
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


def _latched_decision(profile_id, state, now):
    emergency = _emergency(profile_id, state)
    if not emergency:
        return None
    result = _decision(state or _empty(now), now, reason="PROFILE_BLOCKED", message=emergency["message"])
    result.update(browser_status=emergency["browser_status"], safety_persistence_failed=True)
    return result


def _quarantine(conn, profile_id, status, message, now):
    # Page rows retain the public ERROR status for interrupted operations;
    # the profile state separately records the precise RECOVERY_REQUIRED reason.
    page_status = "ERROR" if status == "RECOVERY_REQUIRED" else status
    ids = [row[0] for row in conn.execute(
        "SELECT page_id FROM pages WHERE send_mode='BROWSER' AND antidetect_profile_id=?", (profile_id,))]
    if not ids:
        return
    marks = ",".join("?" for _ in ids)
    conn.execute(f"""UPDATE pages SET browser_status=?,browser_checked_at=NULL,browser_error=?,
                 browser_identity_id=NULL,browser_profile_id=NULL,updated_at=?
                 WHERE page_id IN ({marks}) AND send_mode='BROWSER'""",
                 [page_status, message, now.isoformat(), *ids])
    conn.execute(f"""UPDATE tasks SET status='PAUSED',error_message=?,updated_at=?
                 WHERE status='PENDING' AND page_id IN ({marks})""", [message, now.isoformat(), *ids])


def _recover_state(conn, profile_id, state, now, emergency=None):
    operation = state["operation"]
    if operation and not _is_active(profile_id, operation):
        if emergency:
            state["browser_status"], state["message"] = emergency["browser_status"], emergency["message"]
        elif state["browser_status"] not in _FACEBOOK_BLOCKS:
            state["browser_status"] = "RECOVERY_REQUIRED"
            state["message"] = "Cần kiểm tra profile sau thao tác bị gián đoạn."
        state["generation"] += 1
        state["operation"] = None
        _quarantine(conn, profile_id, state["browser_status"], state["message"], now)
        _write(conn, profile_id, state, now)
    return state


def status(profile_id: str, *, conn=None) -> Dict[str, Any]:
    """Return the current gate and counters without reserving an operation."""
    try:
        key = _profile_key(profile_id)
    except ValueError:
        return _decision(None, _now())
    profile_id = key[len(_PREFIX):]
    now = _now()
    try:
        with closing(db.get_connection()) if conn is None else nullcontext(conn) as connection:
            row = connection.execute("SELECT setting_value FROM app_settings WHERE setting_key=?", (key,)).fetchone()
    except Exception:
        result = _latched_decision(profile_id, None, now) or _decision(None, now)
        result.update(safety_persistence_failed=True)
        return result
    state = _load(row[0], now) if row else _empty(now)
    if state is None:
        return _decision(None, now)
    latched = _latched_decision(profile_id, state, now)
    if latched:
        return latched
    if state["operation"] and not _is_active(profile_id, state["operation"]):
        if state["browser_status"] not in _FACEBOOK_BLOCKS:
            state["browser_status"] = "RECOVERY_REQUIRED"
            state["message"] = "Cần kiểm tra profile sau thao tác bị gián đoạn."
        return _decision(state, now, reason="PROFILE_BLOCKED", message=state["message"])
    max_ops, min_gap = _limits()
    # Day rollover resets only the daily count; the inter-operation gap remains.
    if state["day"] != db.local_day_key(now):
        state["day"] = db.local_day_key(now)
        state["count"] = 0
    result = _decision(state, now, ready=True)
    result["operation_in_progress"] = bool(state["operation"] and _is_active(profile_id, state["operation"]))
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


def _reserve(profile_id: str, allow_blocked: bool, operation=None) -> Dict[str, Any]:
    try:
        key = _profile_key(profile_id)
    except ValueError:
        return _decision(None, _now())
    profile_id = key[len(_PREFIX):]
    now = _now()
    local_day = db.local_day_key(now)
    max_ops, min_gap = _limits()
    try:
        with closing(db.get_connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key=?", (key,)).fetchone()
            state = _load(row[0], now) if row else _empty(now)
            if state is None:
                return _decision(None, now)
            emergency = _emergency(profile_id, state)
            state = _recover_state(conn, profile_id, state, now, emergency)
            if state["operation"] is not None:
                return _decision(state, now, reason="PROFILE_BUSY", message="Profile đang xử lý một thao tác khác.")
            if emergency:
                if not allow_blocked:
                    return _latched_decision(profile_id, state, now)
                state["browser_status"] = emergency["browser_status"]
                state["message"] = emergency["message"]
                state["generation"] += 1
                _quarantine(conn, profile_id, state["browser_status"], state["message"], now)
                _write(conn, profile_id, state, now)
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
            if operation:
                operation["generation"] = state["generation"]
                operation["started_at"] = now.isoformat()
                state["operation"] = operation
            _write(conn, profile_id, state, now)
            # Register before commit so a concurrent read cannot mistake the
            # newly committed marker for an interrupted operation.
            if operation:
                _activate(profile_id, operation["operation_id"])
            result = _decision(state, now, ready=True)
            result["message"] = "Đã giữ lượt thao tác cho profile."
            if operation:
                result.update(operation_id=operation["operation_id"], owner_instance_id=operation["owner_instance_id"])
        if allow_blocked and operation is None:
            with _MEMORY_LOCK:
                _LEGACY_CHECKS[_scope(profile_id)] = result["generation"]
        return result
    except Exception:
        if operation:
            _deactivate(profile_id, operation["operation_id"])
        result = _latched_decision(profile_id, None, now) or _decision(None, now)
        result.update(safety_persistence_failed=True)
        return result


def reserve_operation(profile_id: str, allow_blocked: bool = False) -> Dict[str, Any]:
    """Compatibility budget reservation; Browser I/O must use begin_operation."""
    return _reserve(profile_id, allow_blocked)


def begin_operation(profile_id: str, *, page_id="", kind="check", task_id=None,
                    allow_blocked=False) -> Dict[str, Any]:
    """Commit quota and a crash marker before allowing any Browser I/O."""
    if kind not in {"check", "scan", "verify", "send"} or (allow_blocked and kind != "check"):
        raise ValueError("Loại thao tác profile không hợp lệ")
    operation = {"operation_id": uuid.uuid4().hex, "owner_instance_id": _INSTANCE_ID,
                 "page_id": str(page_id), "kind": kind, "task_id": task_id,
                 "generation": 0, "started_at": _now().isoformat()}
    if not _valid_operation(operation, 0):
        raise ValueError("Dấu thao tác profile không hợp lệ")
    return _reserve(profile_id, allow_blocked, operation)


def abandon_operation(profile_id: str, operation_id: str) -> None:
    """Release only local ownership; keep the durable marker for recovery."""
    profile_id = _profile_key(profile_id)[len(_PREFIX):]
    _deactivate(profile_id, operation_id)


def finish_operation(profile_id: str, operation_id: str, expected_generation: int) -> Dict[str, Any]:
    """Clear only this operation; failed cleanup leaves a durable crash marker."""
    now = _now()
    try:
        profile_id = _profile_key(profile_id)[len(_PREFIX):]
        with closing(db.get_connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key=?", (_profile_key(profile_id),)).fetchone()
            state = _load(row[0], now) if row else None
            if state is None:
                return {"completed": False, "reason": "SAFETY_STATE_INVALID", "safety_persistence_failed": True}
            marker = state["operation"]
            if marker is None:
                return {"completed": True, "reason": "ALREADY_COMPLETED"}
            if (marker["operation_id"] != operation_id or marker["owner_instance_id"] != _INSTANCE_ID
                    or state["generation"] != expected_generation):
                return {"completed": False, "reason": "STALE_OPERATION"}
            if _emergency(profile_id):
                return {"completed": False, "reason": "PROFILE_BLOCKED", "safety_persistence_failed": True}
            state["operation"] = None
            _write(conn, profile_id, state, now)
        return {"completed": True, "reason": "COMPLETED"}
    except Exception:
        if not _emergency(profile_id):
            latch(profile_id, "RECOVERY_REQUIRED", "Không lưu được kết thúc thao tác; cần kiểm tra profile.")
        return {"completed": False, "reason": "SAFETY_PERSISTENCE_FAILED", "safety_persistence_failed": True}
    finally:
        _deactivate(profile_id, operation_id)


def block(profile_id: str, status: str, message: str, *, operation_id=None) -> Dict[str, Any]:
    """Block a profile and quarantine its Browser pages/tasks atomically."""
    if status not in _FACEBOOK_BLOCKS:
        raise ValueError("Trạng thái chặn profile không hợp lệ")
    key = _profile_key(profile_id)
    profile_id = key[len(_PREFIX):]
    now = _now()
    token = latch(profile_id, status, message)
    try:
        with closing(db.get_connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key=?", (key,)).fetchone()
            state = _load(row[0], now) if row else _empty(now)
            if state is not None:
                state["browser_status"] = status
                state["message"] = str(message or "")[:1000]
                state["generation"] += 1
                marker = state["operation"]
                if marker:
                    if operation_id == marker["operation_id"]:
                        state["operation"] = None
                    else:
                        # A late result cannot erase a newer operation. Its
                        # original ticket is invalidated by the generation bump.
                        marker["generation"] = state["generation"]
                _write(conn, profile_id, state, now)
            _quarantine(conn, profile_id, status, str(message or "")[:1000], now)
        if state is None:
            result = _decision(None, now)
            result["safety_persistence_failed"] = True
            return result
        _unlatch(profile_id, token)
        if operation_id:
            _deactivate(profile_id, operation_id)
        result = _decision(state, now, reason="PROFILE_BLOCKED", message=str(message or ""))
        result["safety_persistence_failed"] = False
        return result
    except Exception:
        # Original Facebook evidence remains available even if SQLite fails.
        result = _latched_decision(profile_id, None, now) or _decision(_empty(now), now, reason="PROFILE_BLOCKED", message=str(message or ""))
        result["browser_status"] = status
        result["safety_persistence_failed"] = True
        return result


def clear_block(profile_id: str, expected_generation: int, *, operation_id=None) -> Dict[str, Any]:
    """Clear a block only when the caller holds the current generation token."""
    try:
        key = _profile_key(profile_id)
    except ValueError:
        return {"cleared": False, "reason": "SAFETY_STATE_INVALID"}
    profile_id = key[len(_PREFIX):]
    now = _now()
    emergency = _emergency(profile_id)
    state = None
    failure_status = "RECOVERY_REQUIRED"
    failure_message = "Không lưu được xác nhận khôi phục; profile vẫn bị chặn."
    try:
        with closing(db.get_connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key=?", (key,)).fetchone()
            state = _load(row[0], now) if row else None
            if state is None:
                return {"cleared": False, "reason": "SAFETY_STATE_INVALID"}
            if state["browser_status"] in _FACEBOOK_BLOCKS:
                failure_status, failure_message = state["browser_status"], state["message"]
            if type(expected_generation) is not int or state["generation"] != expected_generation:
                return {"cleared": False, "reason": "STALE_GENERATION", "generation": state["generation"]}
            if state["browser_status"] == "ERROR":
                return {"cleared": False, "reason": "SAFETY_STATE_INVALID", "generation": state["generation"]}
            marker = state["operation"]
            if marker and (marker["operation_id"] != operation_id or marker["kind"] != "check"
                           or not _is_active(profile_id, marker)):
                return {"cleared": False, "reason": "STALE_OPERATION", "generation": state["generation"]}
            if operation_id is not None and marker is None:
                return {"cleared": False, "reason": "STALE_OPERATION", "generation": state["generation"]}
            if marker is None:
                with _MEMORY_LOCK:
                    legacy_check = _LEGACY_CHECKS.get(_scope(profile_id))
                if legacy_check != expected_generation:
                    return {"cleared": False, "reason": "CHECK_REQUIRED", "generation": state["generation"]}
            state["browser_status"] = "READY"
            state["message"] = ""
            state["generation"] += 1
            state["operation"] = None
            _write(conn, profile_id, state, now)
        if emergency:
            _unlatch(profile_id, emergency["token"])
        if operation_id:
            _deactivate(profile_id, operation_id)
        with _MEMORY_LOCK:
            _LEGACY_CHECKS.pop(_scope(profile_id), None)
        return {"cleared": True, "reason": "CLEARED", "generation": state["generation"], "browser_status": "READY"}
    except Exception:
        if not emergency:
            latch(profile_id, failure_status, failure_message)
        return {"cleared": False, "reason": "SAFETY_PERSISTENCE_FAILED", "safety_persistence_failed": True}


def recover_interrupted_operations() -> int:
    """Quarantine crash markers before workers start; persistence errors abort startup."""
    now = _now()
    recovered = 0
    with closing(db.get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT setting_key,setting_value FROM app_settings WHERE setting_key LIKE ?",
                            (_PREFIX + "%",)).fetchall()
        for key, raw in rows:
            profile_id = key[len(_PREFIX):]
            state = _load(raw, now)
            if state is None:
                raise RuntimeError("Trạng thái an toàn Browser không hợp lệ; chưa khởi động worker.")
            marker = state["operation"]
            if marker and not _is_active(profile_id, marker):
                _recover_state(conn, profile_id, state, now, _emergency(profile_id, state))
                recovered += 1
    return recovered
