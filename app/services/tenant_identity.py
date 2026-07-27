"""Tenant brand + sending identity for code that runs outside a request.

Background tasks (morning brief, outbound digest, scheduler confirmations,
invite mail) send email without a `get_tenant_db` session, so they can't
reach `runtime_config.get_org_brand()`. Historically each one hardcoded
BMP's name, colors, domain and hostname instead, which is how BMP branding
leaked into every tenant's mail.

`get_tenant_identity(tenant_id)` is the one lookup those callers should use.

Neutral-by-default: `company_name`, `website_url`, `send_domain`,
`compliance_address` and `logo_url` come back empty when the tenant hasn't
configured them. Empty means empty — never another tenant's identity. A
caller that needs one of these to send must refuse the send (see
`TenantIdentity.can_send_email`), exactly as `email_sender` already does for
the CAN-SPAM postal address.

Colors do have fallbacks, because a missing color can't leak an identity the
way a name can — but they fall back to the neutral palette in NEUTRAL_COLORS,
not to BMP's orange and green.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text

from app.database import async_session

log = logging.getLogger("prospector.tenant_identity")

# The original single-tenant installation. Global env defaults (the send
# domain, the iClosed booking link) are ITS property, so only it may fall
# back to them; every other tenant configures its own or goes without.
PLATFORM_TENANT_ID = 1

# Every tenant is reachable at {slug}.leadprospector.ai even with no custom
# domain on file. Kept in sync with tenancy.PLATFORM_DOMAIN_SUFFIX.
PLATFORM_DOMAIN_SUFFIX = ".leadprospector.ai"

# Neutral palette. Mirrors the column defaults on RuntimeConfig
# (brand_primary_color / brand_secondary_color / brand_accent_bg_color) so a
# tenant that never opens the brand panel looks deliberately unbranded rather
# than looking like BMP.
NEUTRAL_COLORS = {
    "primary_color": "#2563EB",
    "secondary_color": "#1F2937",
    "accent_bg_color": "#F8FAFC",
}

_HEX_RE = __import__("re").compile(r"^#[0-9a-fA-F]{6}$")


@dataclass
class TenantIdentity:
    tenant_id: int
    company_name: str = ""
    website_url: str = ""
    logo_url: str = ""
    compliance_address: str = ""
    send_domain: str = ""
    base_url: str = ""
    primary_color: str = NEUTRAL_COLORS["primary_color"]
    secondary_color: str = NEUTRAL_COLORS["secondary_color"]
    accent_bg_color: str = NEUTRAL_COLORS["accent_bg_color"]

    @property
    def can_send_email(self) -> bool:
        """A tenant can send branded mail once it has both a verified sending
        domain and a name to sign with. Callers should skip rather than fall
        back to a shared domain — sending a client's mail From another
        company's domain is the leak we're closing."""
        return bool(self.send_domain and self.company_name)

    def display_name(self, fallback: str = "") -> str:
        """Tenant name for subject lines and From headers."""
        return self.company_name or fallback

    def missing(self) -> list[str]:
        """Which required-to-send fields are unset — for operator-facing logs."""
        gaps = []
        if not self.send_domain:
            gaps.append("verified sending domain")
        if not self.company_name:
            gaps.append("brand company name")
        if not self.compliance_address:
            gaps.append("compliance postal address")
        return gaps


def _hex(value: Optional[str], fallback: str) -> str:
    v = (value or "").strip()
    return v if _HEX_RE.match(v) else fallback


def shade(hex_color: str, pct: float) -> str:
    """Lighten (pct > 0) or darken (pct < 0) a hex color toward white/black.

    Server-side twin of the `_lpShade` helper the front-end uses to derive
    hover/gradient stops, so emailed HTML and the app derive the same shades
    from one stored brand color instead of hardcoding a second palette.
    """
    if not _HEX_RE.match(hex_color or ""):
        return hex_color
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    target = 255 if pct > 0 else 0
    amount = min(abs(pct), 1.0)
    r, g, b = (round(c + (target - c) * amount) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


async def get_tenant_identity(tenant_id: int) -> TenantIdentity:
    """Brand + sending identity for one tenant. Never raises."""
    ident = TenantIdentity(tenant_id=int(tenant_id))
    try:
        async with async_session() as db:
            row = (await db.execute(text("""
                SELECT brand_company_name, brand_website_url, brand_logo_url,
                       compliance_address, resend_domain_name, resend_domain_status,
                       brand_primary_color, brand_secondary_color, brand_accent_bg_color
                FROM runtime_config
                WHERE tenant_id = :t
                LIMIT 1
            """), {"t": int(tenant_id)})).first()

            if row:
                (company, website, logo, compliance,
                 domain, domain_status, primary, secondary, accent) = row
                ident.company_name = (company or "").strip()
                ident.website_url = (website or "").strip()
                ident.logo_url = (logo or "").strip()
                ident.compliance_address = (compliance or "").strip()
                # Only a VERIFIED domain counts. An unverified one would bounce
                # or spoof, so treat it as unset and let the caller skip.
                if domain and (domain_status or "").lower() == "verified":
                    ident.send_domain = str(domain).strip()
                ident.primary_color = _hex(primary, NEUTRAL_COLORS["primary_color"])
                ident.secondary_color = _hex(secondary, NEUTRAL_COLORS["secondary_color"])
                ident.accent_bg_color = _hex(accent, NEUTRAL_COLORS["accent_bg_color"])

            # settings.send_domain is a single global domain that belongs to the
            # platform tenant, which predates per-tenant Resend domains and so
            # has no verified resend_domain_name of its own. Only tenant 1 may
            # fall back to it — for anyone else that would be sending from
            # another company's domain, which is what this module prevents.
            if not ident.send_domain and int(tenant_id) == PLATFORM_TENANT_ID:
                from app.config import settings
                ident.send_domain = (settings.send_domain or "").strip()

            host = (await db.execute(text("""
                SELECT domain FROM tenant_domains
                WHERE tenant_id = :t
                ORDER BY is_primary DESC, is_verified DESC, id ASC
                LIMIT 1
            """), {"t": int(tenant_id)})).scalar_one_or_none()
            if host:
                ident.base_url = f"https://{str(host).strip().rstrip('/')}"
            else:
                # No custom domain yet. Every tenant is still reachable at
                # {slug}.leadprospector.ai — the host resolver matches that
                # suffix against tenants.slug without needing a tenant_domains
                # row. Falling back to settings.public_url instead would send
                # the tenant's users to the PLATFORM tenant's host, where the
                # login lookup is scoped to that tenant and their account does
                # not exist, so they'd just get a 401.
                slug = (await db.execute(text(
                    "SELECT slug FROM tenants WHERE id = :t LIMIT 1"
                ), {"t": int(tenant_id)})).scalar_one_or_none()
                if slug:
                    ident.base_url = f"https://{str(slug).strip()}{PLATFORM_DOMAIN_SUFFIX}"
    except Exception:
        log.exception("tenant identity lookup failed (tenant=%s)", tenant_id)
    return ident


async def active_tenant_ids() -> list[int]:
    """Every active tenant. For background loops that fan out per tenant."""
    try:
        async with async_session() as db:
            rows = (await db.execute(
                text("SELECT id FROM tenants WHERE status = 'active' ORDER BY id")
            )).scalars().all()
        return [int(r) for r in rows]
    except Exception:
        log.exception("active tenant lookup failed")
        return []


async def tenant_notification_emails(tenant_id: int, roles: tuple[str, ...] = ("admin", "super_admin")) -> list[str]:
    """Admin email addresses for a tenant — the recipients for operational
    reports about that tenant's own data. Replaces hardcoded recipients."""
    try:
        async with async_session() as db:
            rows = (await db.execute(text("""
                SELECT DISTINCT email FROM users
                WHERE tenant_id = :t
                  AND is_active = TRUE
                  AND role = ANY(:roles)
                  AND email <> ''
                ORDER BY email
            """), {"t": int(tenant_id), "roles": list(roles)})).scalars().all()
        return [str(r).strip() for r in rows if str(r or "").strip()]
    except Exception:
        log.exception("tenant notification recipients lookup failed (tenant=%s)", tenant_id)
        return []
