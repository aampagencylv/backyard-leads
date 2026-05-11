"""DNS health monitor — super-admin only.

Background sanity-check that every DNS record we depend on is still
published correctly. Catches the 'Namecheap changed something and our
emails started silently failing SPF' class of bug before it costs a
deliverability week.

Uses Google Public DNS over HTTPS (https://dns.google/resolve) so the
check is independent of the VPS resolver and works the same from any
network. No external lib — just httpx (already a dep).

Each check declares: host to query, record type, an expected substring
the answer should contain, and a severity if the record is missing or
doesn't match. Status rolls up to overall = worst-case across checks.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.auth import require_super_admin
from app.config import settings
from app.models import User

router = APIRouter(prefix="/api/admin/dns", tags=["admin-dns"])
log = logging.getLogger("bmp.dns_health")

DOH_URL = "https://dns.google/resolve"

# Record-type numeric mapping per RFC 1035 + extensions
RR_TYPE = {"A": 1, "TXT": 16, "MX": 15, "CNAME": 5}


def _build_checks() -> list[dict]:
    """Derive the check list from settings so per-tenant SaaS configs
    later just need to swap the env vars."""
    root = settings.reply_domain  # e.g. backyardmarketingpros.com
    mail = settings.send_domain   # e.g. go.backyardmarketingpros.com
    return [
        # Email auth on the sending subdomain
        {
            "label": "SPF (Return-Path / bounce host)",
            "host": f"send.{mail}",
            "type": "TXT",
            "expect_substr": "include:amazonses.com",
            "severity_if_missing": "error",
            "purpose": "Required for SPF to pass on every outbound email — Resend uses Amazon SES under the hood.",
        },
        {
            "label": "DKIM (Resend selector)",
            "host": f"resend._domainkey.{mail}",
            "type": "TXT",
            "expect_substr": "p=",
            "severity_if_missing": "error",
            "purpose": "Resend's public key — required for DKIM signature verification at the receiving mailbox.",
        },
        {
            "label": "DMARC policy",
            "host": f"_dmarc.{root}",
            "type": "TXT",
            "expect_substr": "v=DMARC1",
            "severity_if_missing": "error",
            "purpose": "Tells receiving mailboxes what to do when SPF/DKIM fail. Covers all subdomains via implicit sp= inheritance.",
        },
        {
            "label": "MX (inbound replies)",
            "host": mail,
            "type": "MX",
            "expect_substr": "amazonaws.com",
            "severity_if_missing": "error",
            "purpose": "Resend Inbound runs on AWS SES — replies to r-<token>@" + mail + " route here and get POSTed to /api/email/inbound.",
        },
        {
            "label": "Open-pixel host",
            "host": f"link.{mail}",
            "type": "CNAME",
            "expect_substr": "resend-dns",
            "severity_if_missing": "warn",
            "purpose": "Branded host for Resend's open-tracking pixel. Missing → opens still work, but pixel uses a generic Resend host instead of ours.",
        },
        # Three public surfaces, all A → same VPS
        {
            "label": "App subdomain (operator CRM)",
            "host": _host_of(settings.public_url),
            "type": "A",
            "expect_substr": "",  # any A answer is fine; we just want a hit
            "severity_if_missing": "error",
            "purpose": "Internal CRM. Must resolve for the operator UI to load.",
        },
        {
            "label": "Audit subdomain",
            "host": _host_of(settings.audit_public_url),
            "type": "A",
            "expect_substr": "",
            "severity_if_missing": "error",
            "purpose": "Prospect-facing audit reports embedded in cold emails.",
        },
        {
            "label": "Schedule subdomain",
            "host": _host_of(settings.schedule_public_url),
            "type": "A",
            "expect_substr": "",
            "severity_if_missing": "error",
            "purpose": "Prospect-facing booking pages — CTAs in audit reports + scheduling URLs in emails.",
        },
    ]


def _host_of(url: str) -> str:
    """Strip scheme + path from a config URL → bare hostname."""
    from urllib.parse import urlparse
    return urlparse(url).netloc or url


async def _doh_query(client: httpx.AsyncClient, host: str, rtype: str) -> dict:
    """Single DNS-over-HTTPS lookup. Returns Google's JSON response shape,
    or `{"_error": "..."}` if the request itself failed."""
    try:
        resp = await client.get(
            DOH_URL,
            params={"name": host, "type": rtype},
            headers={"Accept": "application/dns-json"},
            timeout=8.0,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"_error": str(e)}


def _normalize_answer(data: str, rtype: str) -> str:
    """Strip wrapping quotes (TXT) and trailing dots (MX/CNAME) for
    display + substring-matching."""
    if not data:
        return ""
    s = data.strip()
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    if rtype in ("CNAME", "MX") and s.endswith("."):
        s = s[:-1]
    return s


def _evaluate(check: dict, dns_response: dict) -> dict:
    """Turn Google's DoH response into our status format."""
    if "_error" in dns_response:
        return {
            **check,
            "status": "error",
            "value": "",
            "values": [],
            "note": f"DNS lookup failed: {dns_response['_error']}",
        }
    status_code = dns_response.get("Status", -1)
    answers = dns_response.get("Answer") or []
    # Filter to records of the right type (DoH may return mixed types in chains)
    rtype_num = RR_TYPE.get(check["type"], 0)
    matching = [a for a in answers if a.get("type") == rtype_num]
    values = [_normalize_answer(a.get("data", ""), check["type"]) for a in matching]

    if not values:
        # NXDOMAIN, NODATA, or just no answer of the right type
        sev = check.get("severity_if_missing", "error")
        return {
            **check,
            "status": sev,
            "value": "",
            "values": [],
            "note": "Record not published.",
        }

    expect = check.get("expect_substr", "")
    if expect:
        ok = any(expect.lower() in v.lower() for v in values)
        if not ok:
            return {
                **check,
                "status": "warn",
                "value": values[0],
                "values": values,
                "note": f"Record exists but doesn't contain expected substring: {expect!r}",
            }
    return {
        **check,
        "status": "ok",
        "value": values[0],
        "values": values,
        "note": "",
    }


@router.get("/health")
async def dns_health(_user: User = Depends(require_super_admin)) -> dict:
    """Run every DNS check in parallel via DoH. Returns per-check status
    + a rolled-up overall status (worst-case wins: error > warn > ok)."""
    checks = _build_checks()
    async with httpx.AsyncClient() as client:
        responses = await asyncio.gather(*[
            _doh_query(client, c["host"], c["type"]) for c in checks
        ])

    results = [_evaluate(c, r) for c, r in zip(checks, responses)]

    rank = {"ok": 0, "warn": 1, "error": 2}
    worst = max((rank.get(r["status"], 2) for r in results), default=0)
    overall = {0: "ok", 1: "warn", 2: "error"}[worst]

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "doh_provider": "Google Public DNS",
        "summary": {
            "ok":    sum(1 for r in results if r["status"] == "ok"),
            "warn":  sum(1 for r in results if r["status"] == "warn"),
            "error": sum(1 for r in results if r["status"] == "error"),
            "total": len(results),
        },
        "checks": results,
    }
