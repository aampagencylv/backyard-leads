"""
Audit log helper — append-only record of privileged actions.

Goal is "who did what when" answers without grep'ing journalctl.
Captured for security review + SOC2 / enterprise compliance.

Usage from any route:
    from app.services.audit_log import record_audit
    await record_audit(
        db, actor=user, action="user.role_changed",
        target_type="user", target_id=target.id, target_label=target.email,
        metadata={"from": "sales_rep", "to": "admin"},
        request=request,  # FastAPI Request — auto-extracts IP + user-agent
    )

NEVER stores secret values themselves. For runtime_config edits we
record `{"field": "twilio_account_sid", "before_set": True, "after_set": True}`
— enough to reconstruct intent without leaking the keys themselves.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLogEntry, User

log = logging.getLogger("bmp.audit")


async def record_audit(
    db: AsyncSession,
    *,
    actor: Optional[User],
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    target_label: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    request: Optional[Request] = None,
) -> None:
    """Insert one audit-log row. Never raises — auditing failures must
    not break the underlying privileged action. Caller commits the
    surrounding transaction; we just `db.add()` here."""
    try:
        ip = None
        ua = None
        if request is not None:
            ip = request.client.host if request.client else None
            ua = (request.headers.get("user-agent") or "")[:300] or None

        db.add(AuditLogEntry(
            actor_user_id=actor.id if actor else None,
            actor_email=actor.email if actor else None,
            actor_role=actor.role if actor else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_label=(target_label or "")[:255] or None,
            metadata_json=json.dumps(metadata, default=str) if metadata else None,
            ip_address=ip,
            user_agent=ua,
        ))
    except Exception as e:
        log.warning(f"Audit-log write failed for action={action}: {e}")
