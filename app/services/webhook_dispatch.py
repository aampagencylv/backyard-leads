"""
Outbound webhook dispatcher.

When a subscribed event fires, POST the JSON payload to every active
webhook URL that includes that event in its events_json (or has the
empty/null events_json = subscribed to ALL events).

Signing: HMAC-SHA256(secret, raw_body) hex-encoded in
  X-Webhook-Signature: sha256=<hex>
Plus:
  X-Webhook-Event: <event_name>
  X-Webhook-Delivery: <uuid> (for idempotent retry detection on customer side)

Best-effort delivery: ~10s timeout, single attempt for v1. Failed
deliveries log a warning + bump failure_count + record last_delivery_*
fields. v2 adds a retry queue.

NEVER raises — webhook delivery failures must not break the underlying
action (a company create that triggered the webhook still committed).
"""
from __future__ import annotations
import hmac
import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Webhook
from app.database import async_session

log = logging.getLogger("bmp.webhooks")


def _signature(secret: str, body: bytes) -> str:
    """Generate the X-Webhook-Signature header value."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def dispatch_event(
    db: AsyncSession,
    event_name: str,
    data: dict,
    *,
    user_id: Optional[int] = None,
) -> int:
    """Find every active webhook subscribed to event_name + POST to it.
    Returns the number of webhooks attempted.

    Filtering rules:
      - Webhook must be is_active=True
      - When events_json is null OR an empty list → subscribed to ALL events
      - Otherwise events_json must contain event_name

    Multi-tenant note: when user_id is supplied, only webhooks owned by
    that user fire. v1 single-tenant deployments leave it None → all
    webhooks fire (BMP wants every event everywhere it's wired)."""
    q = select(Webhook).where(Webhook.is_active == True)
    if user_id is not None:
        q = q.where(Webhook.user_id == user_id)
    rows = (await db.execute(q)).scalars().all()
    if not rows:
        return 0

    fired = 0
    for hook in rows:
        # Event-list filtering
        if hook.events_json:
            try:
                events = json.loads(hook.events_json)
            except Exception:
                events = []
            if events and event_name not in events:
                continue

        # Background fire-and-forget — don't block the parent transaction
        import asyncio
        asyncio.create_task(_deliver(hook.id, event_name, data))
        fired += 1
    return fired


async def _deliver(webhook_id: int, event_name: str, data: dict) -> None:
    """Background delivery. Opens its own DB session to update the
    webhook's last_delivery_* fields after the attempt completes.
    Never raises — caller has already returned."""
    delivery_id = secrets.token_urlsafe(16)
    body_obj = {
        "event": event_name,
        "delivery_id": delivery_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    body = json.dumps(body_obj, default=str).encode()

    # Re-fetch the webhook so we have the latest secret + url + counters
    async with async_session() as db:
        hook = (await db.execute(select(Webhook).where(Webhook.id == webhook_id))).scalar_one_or_none()
        if not hook or not hook.is_active:
            return
        sig = _signature(hook.secret or "", body)
        url = hook.url
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Webhook-Event": event_name,
                        "X-Webhook-Signature": sig,
                        "X-Webhook-Delivery": delivery_id,
                        "User-Agent": "BMP-Prospector-Webhooks/1.0",
                    },
                )
            hook.last_delivery_at = datetime.now(timezone.utc)
            hook.last_delivery_status = r.status_code
            if 200 <= r.status_code < 300:
                hook.last_delivery_error = None
                hook.failure_count = 0
            else:
                hook.last_delivery_error = f"{r.status_code}: {r.text[:200]}"
                hook.failure_count = (hook.failure_count or 0) + 1
                log.warning(f"Webhook delivery non-2xx {webhook_id} → {url}: {r.status_code}")
        except Exception as e:
            hook.last_delivery_at = datetime.now(timezone.utc)
            hook.last_delivery_status = None
            hook.last_delivery_error = f"network: {str(e)[:200]}"
            hook.failure_count = (hook.failure_count or 0) + 1
            log.warning(f"Webhook delivery failed {webhook_id} → {url}: {e}")
        try:
            await db.commit()
        except Exception:
            pass
