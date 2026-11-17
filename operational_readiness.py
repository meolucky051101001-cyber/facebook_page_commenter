"""Read-only operational snapshots. A snapshot never authorizes a write."""
from contextlib import closing
from datetime import datetime, timezone

import browser_safety
import comment_dispatch
import database as db


TASK_STATUSES = ("PENDING", "SENDING", "SUCCESS", "FAILED", "NEED_CHECK", "CANCELLED", "PAUSED")


def _graph_ready(page):
    return bool(page.get("is_active") and page.get("has_page_token")
                and not comment_dispatch.graph_quarantined(page["page_id"])
                and page.get("token_status") in ("OK", "EXPIRING_SOON")
                and page.get("connection_checked_at") and not page.get("connection_error"))


def snapshot(paused_all, sheet_sync=None):
    """Inspect public metadata and counts, without tokens, reservations or recovery.

    Eligibility describes enabled Pages' saved preparation, independent of Pause.
    Runtime still rechecks policy and current identity before every admission.
    Counts cover all tasks, including NEED_CHECK older than the dashboard window.
    """
    if type(paused_all) is not bool:
        raise ValueError("Unknown run state")
    pages = db.get_all_pages()
    generated_at = datetime.now(timezone.utc).isoformat()
    groups = {}
    for page in pages:
        if page.get("send_mode") == "BROWSER" and page.get("antidetect_profile_id"):
            groups.setdefault(page["antidetect_profile_id"], []).append(page)
    gates = {profile: browser_safety.status(profile) for profile in groups}
    profiles = []
    for profile_id, members in sorted(groups.items()):
        gate = gates[profile_id]
        if gate.get("operation_in_progress") and gate.get("browser_status") == "READY":
            status, reason, message = "WAITING", "PROFILE_BUSY", "Profile đang có thao tác chưa kết thúc."
        elif gate.get("ready") is True:
            status, reason, message = "READY", "READY", None
        elif str(gate.get("reason", "")).startswith("WAIT_PROFILE_"):
            status, reason, message = "WAITING", gate["reason"], gate.get("message")
        else:
            status, reason, message = "BLOCKED", gate.get("reason") or "SAFETY_STATE_INVALID", gate.get("message")
        verified = [p["page_id"] for p in members if comment_dispatch.browser_ready(p, safety=gate)]
        profiles.append({
            "profile_id": profile_id,
            "profile_name": next((p["antidetect_profile_name"] for p in members if p.get("antidetect_profile_name")), profile_id),
            "page_count": len(members), "page_names": [p["page_name"] for p in members],
            "status": status, "reason": reason, "wait_reason": message,
            "retry_at": gate.get("retry_at"), "operations_today": gate.get("count"),
            "operations_limit": gate.get("max_operations_per_day"),
            "blocked_page_ids": [p["page_id"] for p in members] if status == "BLOCKED" else [],
            "verified_page_ids": verified,
        })
    profile_index = {p["profile_id"]: p for p in profiles}
    with closing(db.get_connection()) as conn:
        # Query aggregates only; content, credentials and cookies are not needed.
        counts = dict.fromkeys(TASK_STATUSES, 0)
        for row in conn.execute("SELECT status,COUNT(*) AS n FROM tasks GROUP BY status"):
            if row["status"] in counts:
                counts[row["status"]] = row["n"]
        uncertain = {row["page_id"]: row["n"] for row in conn.execute(
            "SELECT page_id,COUNT(*) AS n FROM tasks WHERE status='NEED_CHECK' GROUP BY page_id")}
    findings = []
    blocked_pages, attention = [], []
    eligible, sources, enabled = 0, 0, 0

    def finding(code, reason, page=None, severity="error"):
        item = {"severity": severity, "code": code, "reason": reason,
                "page_id": page["page_id"] if page else None,
                "page_name": page["page_name"] if page else None,
                "profile_id": page.get("antidetect_profile_id") if page and page.get("send_mode") == "BROWSER" else None}
        findings.append(item)
        return item

    if paused_all:
        finding("SYSTEM_PAUSED", "Hệ thống vẫn tạm dừng nhận lượt gửi mới.", severity="info")
    sheet_error = (sheet_sync or {}).get("last_error")
    if sheet_error:
        finding("SHEET_LAST_ERROR", str(sheet_error), severity="warning")
    for page in pages:
        page_findings = []
        if page.get("rule_enabled"):
            enabled += 1
        source_error = page.get("source_error")
        if not source_error and sheet_sync is not None and not sheet_sync.get("sheet_url"):
            source_error = "Chưa cấu hình đường dẫn Sheet bình luận."
        if source_error or not page.get("source_synced_at"):
            sources += 1
            page_findings.append(finding("SOURCE_INVALID", source_error or "Page chưa có cấu hình Sheet đã đồng bộ.", page))
        transport = page.get("send_mode")
        if transport == "BROWSER":
            profile = profile_index.get(page.get("antidetect_profile_id"))
            gate = gates.get(page.get("antidetect_profile_id"))
            if not profile:
                page_findings.append(finding("PROFILE_MISSING", "Page chưa ghép profile Browser.", page))
            elif profile["status"] != "READY":
                page_findings.append(finding(profile["reason"], profile["wait_reason"] or "Profile chưa sẵn sàng.", page,
                                             "warning" if profile["status"] == "WAITING" else "error"))
            if gate is None or not comment_dispatch.browser_ready(page, safety=gate):
                page_findings.append(finding("PAGE_PROOF_REQUIRED", page.get("browser_error") or "Cần xác minh lại đúng Page trong profile đã chọn.", page))
        elif transport == "GRAPH_API":
            if not _graph_ready(page):
                page_findings.append(finding("PAGE_PROOF_REQUIRED", page.get("connection_error") or page.get("status_note") or "Cần xác minh kết nối Graph của Page.", page))
        else:
            page_findings.append(finding("SEND_MODE_INVALID", "Cách gửi của Page chưa hợp lệ.", page))
        # Reuse the dispatch projection with Pause excluded so global Pause cannot
        # hide Page/profile cooldowns. This only reads counters and metadata.
        status = comment_dispatch.page_status(page, paused=False, safety=gates.get(page.get("antidetect_profile_id")))
        if status.get("next_send_at"):
            page_findings.append(finding("PAGE_WAIT", status.get("send_wait_reason") or "Page đang chờ hạn mức hoặc khoảng cách gửi.", page, "warning"))
        if uncertain.get(page["page_id"]):
            # Uncertainty blocks that task/sequence, not every unrelated task.
            finding("NEED_CHECK", f"Có {uncertain[page['page_id']]} tác vụ cần đối chiếu trước khi khôi phục.", page, "warning")
        if page_findings:
            brief = {key: page_findings[0][key] for key in ("page_id", "page_name", "profile_id", "reason")}
            blocked_pages.append(brief)
            attention.append(brief)
        elif page.get("rule_enabled"):
            eligible += 1
    safety_report = {"profiles": profiles, "blocked_pages": blocked_pages,
                     "pages_needing_attention": attention, "paused_all": paused_all,
                     "read_only": True, "generated_at": generated_at}
    preflight = {"read_only": True, "scope": "catalog", "generated_at": generated_at,
                 "paused_all": paused_all, "eligible_pages": eligible, "enabled_pages": enabled,
                 "blocked_pages": len(blocked_pages), "blocked_profiles": sum(p["status"] == "BLOCKED" for p in profiles),
                 "source_errors": sources, "needs_check_count": counts["NEED_CHECK"],
                 "task_counts": counts, "findings": findings}
    return {"profile_safety": safety_report, "preflight": preflight}
