"""
Resend sending-domain provisioning for new tenants.

For each tenant we create a per-tenant subdomain under leadprospector.ai
(e.g. `go.acme.leadprospector.ai`) registered with Resend. Resend
returns the SPF / DKIM / DMARC records that need to land in DNS for the
domain to verify. We store the records on the tenant's RuntimeConfig so
the platform admin can copy them into leadprospector.ai's DNS provider
in one trip.

White-labeled by design — the customer never sees the word "Resend"; the
admin UI calls it "Email sending".

Configuration:
  RESEND_API_KEY        — env var, the platform's Resend API key (already
                          present for BMP's sending)

When unset, provisioning is a silent no-op. The operator can attach a
domain later from the admin console.
"""
from __future__ import annotations
import logging
import os
from typing import Optional, TypedDict

import httpx

log = logging.getLogger("bmp.resend_provisioning")


class ResendProvisionResult(TypedDict):
    domain_id: str
    domain_name: str
    records: list[dict]   # raw DNS records from Resend
    status: str           # not_started | pending | verified | failed


def _api_key() -> Optional[str]:
    """Resolve which Resend API key to use for tenant domain provisioning.

    Order of preference:
      1. PLATFORM_RESEND_API_KEY — the LeadProspector Resend workspace
         (where every new tenant's go.{slug}.leadprospector.ai should
         live; platform pays the Resend bill, tenant pays platform)
      2. RESEND_API_KEY — fallback to BMP's existing Resend account.
         Useful pre-platform-Resend setup; once PLATFORM_RESEND_API_KEY
         is set, new tenant domains land in the right account.
    """
    platform = (os.environ.get("PLATFORM_RESEND_API_KEY") or "").strip()
    if platform:
        return platform
    fallback = (os.environ.get("RESEND_API_KEY") or "").strip()
    return fallback or None


def is_configured() -> bool:
    return _api_key() is not None


PLATFORM_BASE_DOMAIN = "leadprospector.ai"


def is_platform_domain(domain_name: str) -> bool:
    """True when the sending domain is under leadprospector.ai (DNS we
    control and can auto-add via Cloudflare). False for a customer's own
    domain like go.aamp.agency, where the customer must add the records
    to their own DNS."""
    d = (domain_name or "").strip().lower().rstrip(".")
    return d == PLATFORM_BASE_DOMAIN or d.endswith("." + PLATFORM_BASE_DOMAIN)


async def register_domain(domain_name: str) -> Optional[ResendProvisionResult]:
    """Register ANY sending domain in Resend and return its id + the
    DKIM/SPF/DMARC records that must land in DNS.

    `domain_name` is the FULL domain — e.g. "go.aamp.agency" (a customer's
    own domain) or "go.acme.leadprospector.ai" (a platform-hosted one).

    For a customer domain, the records are what the customer adds to THEIR
    DNS. For a platform domain, the caller can auto-add them via Cloudflare.

    Idempotent-ish: if the domain already exists in Resend, the API returns
    a 4xx; the caller can fall back to looking it up. Never raises.
    """
    api_key = _api_key()
    if not api_key:
        log.info("resend domain skipped — neither PLATFORM_RESEND_API_KEY nor RESEND_API_KEY set")
        return None

    name = (domain_name or "").strip().lower().rstrip(".")
    if not name or "." not in name:
        log.warning(f"resend register_domain: invalid domain {domain_name!r}")
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"name": name, "region": "us-east-1"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post("https://api.resend.com/domains", json=payload, headers=headers)
        if r.status_code >= 400:
            log.warning(f"resend domain create failed: {r.status_code} {r.text[:240]}")
            return None
        data = r.json()
        domain_id = data.get("id") or data.get("data", {}).get("id")
        resolved_name = data.get("name") or data.get("data", {}).get("name") or name
        records = data.get("records") or data.get("data", {}).get("records") or []
        status = data.get("status") or "not_started"
        if not domain_id:
            log.warning(f"resend response missing id: {data}")
            return None
        log.info(f"resend domain provisioned: {domain_id} ({resolved_name})")
        return {
            "domain_id": domain_id,
            "domain_name": resolved_name,
            "records": records,
            "status": status,
        }
    except Exception:
        log.exception("resend domain provisioning raised; ignoring")
        return None


async def create_domain(subdomain: str) -> Optional[ResendProvisionResult]:
    """Platform-subdomain convenience wrapper used by tenant auto-create.

    `subdomain` is the LEAF only — e.g. "go.acme" produces
    "go.acme.leadprospector.ai". For arbitrary/custom domains call
    register_domain(full_domain) instead.
    """
    return await register_domain(f"{subdomain}.{PLATFORM_BASE_DOMAIN}")


async def trigger_verify(domain_id: str) -> Optional[dict]:
    """Ask Resend to re-check DNS and (re)verify the domain. Call this
    AFTER the DNS records have been added — Resend won't poll on its own.
    Returns the raw response (or None). Never raises."""
    api_key = _api_key()
    if not api_key:
        return None
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"https://api.resend.com/domains/{domain_id}/verify", headers=headers
            )
        if r.status_code >= 400:
            log.warning(f"resend domain verify failed: {r.status_code} {r.text[:200]}")
            return None
        return r.json() if r.content else {"ok": True}
    except Exception:
        log.exception("resend trigger_verify raised; ignoring")
        return None


async def set_open_tracking(domain_id: str, *, open_tracking: bool = True,
                            click_tracking: bool = False) -> Optional[dict]:
    """Toggle Resend's per-domain open/click tracking. We enable OPEN tracking
    (Resend injects the pixel → email.opened webhook) but leave CLICK tracking
    OFF, because we do our own /t/{token} click-wrapping (enabling both would
    double-wrap links). Call right after a domain is registered. Never raises."""
    api_key = _api_key()
    if not api_key:
        return None
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.patch(
                f"https://api.resend.com/domains/{domain_id}",
                json={"open_tracking": open_tracking, "click_tracking": click_tracking},
                headers=headers,
            )
        if r.status_code >= 400:
            log.warning(f"resend set_open_tracking failed: {r.status_code} {r.text[:200]}")
            return None
        return r.json() if r.content else {"ok": True}
    except Exception:
        log.exception("resend set_open_tracking raised; ignoring")
        return None


async def get_domain_status(domain_id: str) -> Optional[dict]:
    """Fetch the current verification status + records for a domain.
    Used by the admin UI to display whether DNS has propagated yet."""
    api_key = _api_key()
    if not api_key:
        return None
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"https://api.resend.com/domains/{domain_id}", headers=headers)
        if r.status_code >= 400:
            log.warning(f"resend domain status fetch failed: {r.status_code} {r.text[:200]}")
            return None
        return r.json()
    except Exception:
        log.exception("resend get_domain_status raised; ignoring")
        return None
