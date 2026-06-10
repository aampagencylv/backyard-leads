"""Pull-based inbound email ingestion — the reliable half of reply routing.

Outbound emails carry Reply-To r-<token>@go.backyardmarketingpros.com.
Resend's inbound MX receives replies and stores them (verified working),
but its email.received webhook push has NEVER delivered an event to us —
12 real prospect replies sat invisible from May 12 to Jun 10 2026 while
the team believed nobody ever replied.

This poller runs on the app tick: lists Resend's inbound store, feeds
anything new through the same process_inbound_payload() the webhook uses
(auto-pause sequence, reply Activity, status flip, forward to the BDR's
real inbox), and stamps an `inbound_email_ingested` Activity per Resend
id as the idempotency marker — so webhook deliveries (if Resend ever
fixes push) and repeated polls can't double-process.
"""
from __future__ import annotations
import json
import logging

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

log = logging.getLogger("bmp.inbound_poller")


async def poll_resend_inbound(db: AsyncSession, *, limit: int = 100) -> dict:
    """Ingest unprocessed inbound emails from Resend. Returns counters."""
    counters = {"listed": 0, "ingested": 0, "skipped_done": 0, "errors": 0}
    if not settings.resend_api_key:
        return counters

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"https://api.resend.com/emails/inbound?limit={limit}",
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            )
        if r.status_code != 200:
            log.warning(f"inbound poll: list failed HTTP {r.status_code}: {r.text[:200]}")
            counters["errors"] += 1
            return counters
        items = r.json().get("data", []) or []
    except httpx.HTTPError as e:
        log.warning(f"inbound poll: list failed: {e}")
        counters["errors"] += 1
        return counters

    counters["listed"] = len(items)
    for item in items:
        rid = item.get("id")
        if not rid:
            continue
        done = (await db.execute(text("""
            SELECT 1 FROM activities
            WHERE activity_type = 'inbound_email_ingested'
              AND metadata_json LIKE :marker
            LIMIT 1
        """), {"marker": f'%"resend_inbound_id": "{rid}"%'})).first()
        if done is not None:
            counters["skipped_done"] += 1
            continue

        payload = {
            "type": "email.received",
            "data": {
                "email_id": rid,
                "to": item.get("to") or [],
                "cc": item.get("cc") or [],
                "from": item.get("from") or "",
                "subject": item.get("subject") or "",
                "message_id": item.get("message_id"),
            },
        }
        try:
            from app.routes.email_inbound_routes import process_inbound_payload
            result = await process_inbound_payload(payload)
            summary = result if isinstance(result, dict) else {"status": "processed"}
        except Exception as e:
            counters["errors"] += 1
            log.exception(f"inbound poll: processing {rid} failed: {e}")
            # No marker written — retried next tick. A poison message that
            # fails forever just logs once per tick; acceptable volume.
            continue

        # Stamp the idempotency marker in its own transaction so a webhook
        # arriving mid-poll can't race a duplicate ingestion next tick.
        await db.execute(text("""
            INSERT INTO activities (tenant_id, activity_type, content, metadata_json, created_at)
            VALUES (1, 'inbound_email_ingested', :content, :meta, NOW())
        """), {
            "content": f"Ingested inbound email from {item.get('from','?')[:80]}",
            "meta": json.dumps({
                "resend_inbound_id": rid,
                "from": item.get("from"),
                "subject": (item.get("subject") or "")[:140],
                "result": summary,
            }, default=str),
        })
        await db.commit()
        counters["ingested"] += 1

    if counters["ingested"]:
        log.info(f"inbound poll: {counters}")
    return counters
