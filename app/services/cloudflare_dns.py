"""
Cloudflare DNS automation for leadprospector.ai.

When we provision a new tenant, Resend returns ~4 DNS records (MX/SPF/DKIM)
that need to land in DNS for the sending domain to verify. Rather than
the platform admin copy-pasting them into Cloudflare's UI, this module
adds them automatically via the Cloudflare API.

Scope (intentionally narrow):
  - Add a DNS record to leadprospector.ai zone
  - Delete a DNS record (for tenant offboarding)
  - List records (for verification)

Configuration (VPS env):
  CLOUDFLARE_API_TOKEN  — scoped token with Zone → DNS → Edit on
                          leadprospector.ai only. Generate at
                          dash.cloudflare.com/profile/api-tokens
  CLOUDFLARE_ZONE_ID    — leadprospector.ai's zone id (visible on the
                          zone overview page in Cloudflare)

When unset, every function returns None / False and logs. Never raises.
Mirrors the resend_provisioning / twilio_provisioning pattern.
"""
from __future__ import annotations
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("bmp.cloudflare_dns")

_API_BASE = "https://api.cloudflare.com/client/v4"


def _config() -> Optional[dict]:
    token = (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
    zone = (os.environ.get("CLOUDFLARE_ZONE_ID") or "").strip()
    if not token or not zone:
        return None
    return {"token": token, "zone_id": zone}


def is_configured() -> bool:
    return _config() is not None


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def add_record(*, name: str, record_type: str, content: str,
                     priority: Optional[int] = None, ttl: int = 1) -> Optional[str]:
    """Add a DNS record. Returns the record id on success, None otherwise.

    `ttl=1` is Cloudflare's "Auto" — recommended for most records so
    Cloudflare handles caching. Use a higher value for records that
    don't change often if you want stricter control.

    `priority` is required for MX records, ignored for others.

    Idempotency: if a record with the same (name, type, content) already
    exists, we treat it as success and return the existing id. Resend's
    DNS records don't change between calls, so this is the right shape.
    """
    cfg = _config()
    if not cfg:
        log.info(f"cloudflare add_record skipped — CLOUDFLARE_* not set ({record_type} {name})")
        return None

    payload: dict = {
        "type": record_type,
        "name": name,
        "content": content,
        "ttl": ttl,
        "proxied": False,  # DNS records (MX/TXT/CNAME) cannot be proxied through CF anyway
    }
    if priority is not None and record_type == "MX":
        payload["priority"] = priority

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{_API_BASE}/zones/{cfg['zone_id']}/dns_records",
                headers=_headers(cfg["token"]),
                json=payload,
            )
        data = r.json()
        if data.get("success"):
            rec_id = data.get("result", {}).get("id")
            log.info(f"cloudflare added: {record_type} {name} -> {content[:60]} (id={rec_id})")
            return rec_id

        # Failure path: check if it's "record already exists" — treat as success.
        errors = data.get("errors") or []
        for err in errors:
            if err.get("code") == 81057 or "already exists" in (err.get("message") or "").lower():
                existing = await _find_record(cfg, name, record_type, content)
                if existing:
                    log.info(f"cloudflare exists already: {record_type} {name} (id={existing})")
                    return existing

        log.warning(f"cloudflare add failed: {data.get('errors')}")
        return None
    except Exception:
        log.exception("cloudflare add_record raised")
        return None


async def _find_record(cfg: dict, name: str, record_type: str,
                       content: Optional[str] = None) -> Optional[str]:
    """Look up a record id by (name, type[, content])."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{_API_BASE}/zones/{cfg['zone_id']}/dns_records",
                headers={"Authorization": f"Bearer {cfg['token']}"},
                params={"type": record_type, "name": name},
            )
        data = r.json()
        for rec in data.get("result", []):
            if content is None or rec.get("content") == content:
                return rec.get("id")
        return None
    except Exception:
        log.exception("cloudflare _find_record raised")
        return None


async def delete_record(record_id: str) -> bool:
    """Remove a DNS record by id. Returns True on success."""
    cfg = _config()
    if not cfg:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.delete(
                f"{_API_BASE}/zones/{cfg['zone_id']}/dns_records/{record_id}",
                headers={"Authorization": f"Bearer {cfg['token']}"},
            )
        if r.status_code in (200, 204):
            log.info(f"cloudflare deleted: {record_id}")
            return True
        log.warning(f"cloudflare delete failed: {r.status_code} {r.text[:200]}")
        return False
    except Exception:
        log.exception("cloudflare delete_record raised")
        return False


async def add_resend_records(records: list[dict]) -> list[str]:
    """Bulk-add the DNS records that Resend returned when a domain was
    provisioned. Returns the list of CF record ids that landed.

    Each Resend record looks like:
      {"type": "MX", "name": "send", "value": "feedback-smtp...", "priority": 10}
      {"type": "TXT", "name": "send", "value": "v=spf1 include:..."}
      {"type": "TXT", "name": "resend._domainkey", "value": "p=MIGfMA..."}

    Resend's `name` is RELATIVE to the parent domain — Cloudflare's API
    accepts either the bare relative name or the full hostname. We send
    the full hostname (relative + ".leadprospector.ai") for clarity.
    """
    if not is_configured():
        return []
    parent = "leadprospector.ai"
    out: list[str] = []
    for rec in records:
        rtype = rec.get("type", "").upper()
        leaf = (rec.get("name") or "").strip(".")
        full_name = parent if not leaf else f"{leaf}.{parent}"
        content = rec.get("value") or ""
        priority = rec.get("priority")
        record_id = await add_record(
            name=full_name,
            record_type=rtype,
            content=content,
            priority=priority,
        )
        if record_id:
            out.append(record_id)
    return out
