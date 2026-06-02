"""
Integration management endpoints — API keys + webhook subscriptions.

These power the Settings → Integrations UI. Distinct from the public
/api/v1/* surface, which is what external integrations actually call.

All endpoints require login (admin+ for visibility into other users'
keys/hooks; sales_rep can manage their own).
"""
from __future__ import annotations
import hashlib
import json
import secrets
from typing import Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.tenancy import get_tenant_db
from app.models import User, ApiKey, Webhook
from app.auth import get_current_user, require_admin

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


# ============================================================
# API Keys
# ============================================================

def _key_payload(k: ApiKey) -> dict:
    return {
        "id": k.id,
        "user_id": k.user_id,
        "name": k.name,
        "key_prefix": k.key_prefix,
        "is_active": k.is_active,
        "scope": getattr(k, "scope", "read") or "read",
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "created_at": k.created_at.isoformat() if k.created_at else None,
    }


@router.get("/api-keys")
async def list_api_keys(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """List the caller's keys. Admins additionally see all keys for
    audit purposes (with the same payload — never reveals the secret
    since we only store hashes)."""
    q = select(ApiKey).order_by(ApiKey.created_at.desc())
    if user.role not in ("admin", "super_admin"):
        q = q.where(ApiKey.user_id == user.id)
    rows = (await db.execute(q)).scalars().all()
    return [_key_payload(r) for r in rows]


class CreateApiKeyRequest(BaseModel):
    name: str
    # 'read'  → search/get/summarize tools only (default; safe)
    # 'write' → can also invoke MCP write tools (add note, enroll
    #           in sequence, book meeting, send email, etc.)
    scope: Optional[str] = "read"


@router.post("/api-keys")
async def create_api_key(
    req: CreateApiKeyRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Generate a new API key. Returns the plaintext ONCE — caller
    must save it. We only persist a SHA-256 hash + a 12-char prefix
    for display."""
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    scope = (req.scope or "read").strip().lower()
    if scope not in ("read", "write"):
        raise HTTPException(status_code=400, detail="scope must be 'read' or 'write'")
    plaintext = "pk_live_" + secrets.token_hex(32)
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    prefix = plaintext[:12] + "..."
    row = ApiKey(
        user_id=user.id,
        name=req.name.strip()[:80],
        key_hash=key_hash,
        key_prefix=prefix,
        is_active=True,
        scope=scope,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    # Return plaintext exactly once. Stripe-style.
    return {
        **_key_payload(row),
        "plaintext_key": plaintext,
        "warning": "This is the ONLY time the full key is shown. Save it now.",
    }


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Soft-delete by setting is_active=False. Hard delete intentionally
    not exposed — preserves audit history of who used the key when."""
    row = (await db.execute(select(ApiKey).where(ApiKey.id == key_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Key not found")
    if row.user_id != user.id and user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Cannot revoke another user's key")
    row.is_active = False
    await db.commit()
    return {"ok": True, "id": row.id, "is_active": row.is_active}


# ============================================================
# Webhooks
# ============================================================

KNOWN_EVENTS = (
    "company.created",
    "company.merged",
    "company.deleted",
    "contact.created",
    "email.replied",
    "meeting.booked",
    "sequence.created",
    "deal.stage_changed",
)


def _webhook_payload(w: Webhook, *, include_secret: bool = False) -> dict:
    out = {
        "id": w.id,
        "user_id": w.user_id,
        "name": w.name,
        "url": w.url,
        "events": json.loads(w.events_json) if w.events_json else [],
        "is_active": w.is_active,
        "last_delivery_at": w.last_delivery_at.isoformat() if w.last_delivery_at else None,
        "last_delivery_status": w.last_delivery_status,
        "last_delivery_error": w.last_delivery_error,
        "failure_count": w.failure_count or 0,
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }
    if include_secret:
        out["secret"] = w.secret
    return out


@router.get("/webhooks")
async def list_webhooks(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    q = select(Webhook).order_by(Webhook.created_at.desc())
    if user.role not in ("admin", "super_admin"):
        q = q.where(Webhook.user_id == user.id)
    rows = (await db.execute(q)).scalars().all()
    return [_webhook_payload(r) for r in rows]


class CreateWebhookRequest(BaseModel):
    name: str
    url: str
    events: Optional[list[str]] = None  # empty/None = all events


@router.post("/webhooks")
async def create_webhook(
    req: CreateWebhookRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    if not req.url or not req.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url must be a full http(s) URL")
    invalid_events = [e for e in (req.events or []) if e not in KNOWN_EVENTS]
    if invalid_events:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown events: {invalid_events}. Valid: {list(KNOWN_EVENTS)}",
        )
    secret = "whsec_" + secrets.token_urlsafe(32)
    row = Webhook(
        user_id=user.id,
        name=req.name.strip()[:80],
        url=req.url.strip()[:500],
        secret=secret,
        events_json=json.dumps(req.events) if req.events else None,
        is_active=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    # Return secret once for the caller to copy into their endpoint
    return {**_webhook_payload(row, include_secret=True),
            "warning": "This is the ONLY time the signing secret is shown. Save it now."}


class UpdateWebhookRequest(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    events: Optional[list[str]] = None
    is_active: Optional[bool] = None


@router.patch("/webhooks/{webhook_id}")
async def update_webhook(
    webhook_id: int,
    req: UpdateWebhookRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    row = (await db.execute(select(Webhook).where(Webhook.id == webhook_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook not found")
    if row.user_id != user.id and user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Not your webhook")
    if req.name is not None:
        row.name = req.name.strip()[:80]
    if req.url is not None:
        if not req.url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="url must be a full http(s) URL")
        row.url = req.url.strip()[:500]
    if req.events is not None:
        invalid = [e for e in req.events if e not in KNOWN_EVENTS]
        if invalid:
            raise HTTPException(status_code=400, detail=f"Unknown events: {invalid}")
        row.events_json = json.dumps(req.events) if req.events else None
    if req.is_active is not None:
        row.is_active = bool(req.is_active)
    await db.commit()
    return _webhook_payload(row)


@router.delete("/webhooks/{webhook_id}")
async def delete_webhook(
    webhook_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    row = (await db.execute(select(Webhook).where(Webhook.id == webhook_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook not found")
    if row.user_id != user.id and user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Not your webhook")
    await db.delete(row)
    await db.commit()
    return {"ok": True, "id": webhook_id}


@router.post("/webhooks/{webhook_id}/test")
async def test_webhook(
    webhook_id: int,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Fire a synthetic 'webhook.test' event to the configured URL.
    Useful for verifying signature handling on the customer side
    without needing to trigger a real CRM event."""
    row = (await db.execute(select(Webhook).where(Webhook.id == webhook_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook not found")
    if row.user_id != user.id and user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Not your webhook")
    from app.services.webhook_dispatch import _deliver
    import asyncio
    asyncio.create_task(_deliver(row.id, "webhook.test",
                                 {"message": "Test ping from BMP Prospector",
                                  "triggered_by": user.email}))
    return {"sent": True, "message": "Test event queued — check your endpoint logs"}


@router.get("/known-events")
async def list_known_events(
    user: User = Depends(get_current_user),
):
    """Returns the list of event names webhooks can subscribe to."""
    return {"events": list(KNOWN_EVENTS)}
