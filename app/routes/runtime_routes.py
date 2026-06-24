"""
Org-level runtime config endpoints — Netrows + Twilio API keys.
Surfaced in the Settings UI so the team can rotate keys without SSH.
"""
from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.tenancy import get_tenant_db
from app.models import User
from app.auth import get_current_user
from app.services.audit_log import record_audit
from fastapi import Request
from app.runtime_config import (
    _get_or_create,
    set_netrows_api_key,
    set_twilio_credentials,
    set_deepgram_api_key,
    set_blooio_api_key,
    set_blooio_signing_secret,
    set_resend_webhook_secret,
    set_messaging_direction,
    set_apollo_api_key,
    set_google_maps_api_key,
    set_audit_branding,
    set_org_brand,
    get_org_brand,
    DEFAULT_MESSAGING_DIRECTION,
    mask_key,
)

router = APIRouter(prefix="/api", tags=["runtime-config"])


class UpdateRuntimeConfigRequest(BaseModel):
    netrows_api_key: Optional[str] = None
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_api_key_sid: Optional[str] = None
    twilio_api_key_secret: Optional[str] = None
    twilio_twiml_app_sid: Optional[str] = None
    deepgram_api_key: Optional[str] = None
    blooio_api_key: Optional[str] = None
    blooio_signing_secret: Optional[str] = None
    resend_webhook_secret: Optional[str] = None
    messaging_direction: Optional[str] = None
    apollo_api_key: Optional[str] = None  # Tenant-tier (admin can set)
    google_maps_api_key: Optional[str] = None  # Platform-tier (super_admin only)
    audit_report_header_url: Optional[str] = None  # Tenant-tier (admin can set)
    audit_report_logo_url: Optional[str] = None    # Tenant-tier (admin can set)
    audit_left_image_url: Optional[str] = None
    audit_left_message: Optional[str] = None
    audit_right_image_url: Optional[str] = None
    audit_right_message: Optional[str] = None
    audit_scheduler_type: Optional[str] = None    # 'iclosed' | 'native' | 'custom'
    audit_native_user_id: Optional[int] = None
    audit_custom_url: Optional[str] = None
    # Org-wide brand — single source of truth for the tenant's identity
    brand_primary_color: Optional[str] = None
    brand_secondary_color: Optional[str] = None
    brand_accent_bg_color: Optional[str] = None
    brand_logo_url: Optional[str] = None
    brand_company_name: Optional[str] = None
    brand_website_url: Optional[str] = None
    # iMessage send toggle. When False, sequence engine skips all iMessage
    # steps for this tenant instead of attempting + failing on Blooio.
    imessage_enabled: Optional[bool] = None


def _mask_field(field: Optional[str]) -> dict:
    """Helper used by tenant-tier integration blocks to mirror the
    {set, masked} shape the existing Settings UI renders."""
    v = (field or "").strip()
    return {"set": bool(v), "masked": mask_key(v)}


def _tenant_payload(rc, settings_obj) -> dict:
    """Tenant-tier config — admins read + edit.

    SaaS model: the platform supplies enrichment / carrier / AI / email
    infrastructure. The ONE customer-supplied integration is Apollo —
    tenants who already pay for Apollo can plug their key in to layer
    Apollo's data over the platform-supplied Netrows + Hunter results.

    AI tone (messaging direction) is also tenant-tier so admins can shape
    their team's voice without escalating to platform-level access.
    """
    apollo_key = (rc.apollo_api_key or "").strip()
    return {
        "brand": {
            "primary_color":   getattr(rc, "brand_primary_color", None) or "#E65100",
            "secondary_color": getattr(rc, "brand_secondary_color", None) or "#1B5E20",
            "accent_bg_color": getattr(rc, "brand_accent_bg_color", None) or "#FFF8F0",
            "logo_url":        getattr(rc, "brand_logo_url", None) or "",
            "company_name":    getattr(rc, "brand_company_name", None) or "Backyard Marketing Pros",
            "website_url":     getattr(rc, "brand_website_url", None) or "https://backyardmarketingpros.com",
        },
        "apollo": {
            "set": bool(apollo_key),
            "masked": mask_key(apollo_key),
        },
        "messaging": {
            "direction": (rc.messaging_direction or "").strip(),
            "is_custom": bool((rc.messaging_direction or "").strip()),
            "default_preview": DEFAULT_MESSAGING_DIRECTION[:240] + "…",
        },
        "audit_branding": {
            "header_url": (getattr(rc, "audit_report_header_url", None) or ""),
            "logo_url": (getattr(rc, "audit_report_logo_url", None) or ""),
            "left_image_url":  (getattr(rc, "audit_left_image_url", None) or ""),
            "left_message":    (getattr(rc, "audit_left_message", None) or ""),
            "right_image_url": (getattr(rc, "audit_right_image_url", None) or ""),
            "right_message":   (getattr(rc, "audit_right_message", None) or ""),
            "scheduler_type":  (getattr(rc, "audit_scheduler_type", None) or "iclosed"),
            "native_user_id":  getattr(rc, "audit_native_user_id", None),
            "custom_url":      (getattr(rc, "audit_custom_url", None) or ""),
        },
        # Provisioning status — tenant admin sees high-level readiness for
        # their outbound channels (without seeing the underlying DKIM keys
        # or sub-account auth_token, which stay platform-tier).
        "sending_domain": {
            "configured": bool((getattr(rc, "resend_domain_id", None) or "").strip()),
            "name":   (getattr(rc, "resend_domain_name", None) or ""),
            "status": (getattr(rc, "resend_domain_status", None) or ""),
        },
        "voice_account": {
            "configured": bool((getattr(rc, "twilio_account_sid", None) or "").strip()),
        },
        # Channel toggles — operator-controlled. When False, the sequence
        # engine auto-skips steps for that channel instead of attempting.
        "channels": {
            "imessage_enabled": bool(getattr(rc, "imessage_enabled", False)),
        },
    }


def _platform_payload(rc, settings_obj) -> dict:
    """Platform-tier config — super_admin only.

    Every integration credential lives here. Tenant admins should not
    see API keys / Twilio creds / Blooio keys in their Settings UI;
    they use these services (phone-number purchase, sending, dialing)
    via API endpoints that read the credentials internally without
    exposing them. The platform admin manages keys from /admin's
    per-tenant API Keys vault.
    """
    def t(field: str | None) -> dict:
        v = (field or "").strip()
        return {"set": bool(v), "masked": mask_key(v)}

    netrows_db = (rc.netrows_api_key or "").strip()
    netrows_env = settings_obj.netrows_api_key or ""
    netrows_eff = netrows_db or netrows_env

    return {
        "netrows": {
            "set": bool(netrows_eff),
            "source": "database" if netrows_db else ("env" if netrows_env else "none"),
            "masked": mask_key(netrows_eff),
            "updated_at": rc.updated_at.isoformat() if rc.updated_at else None,
        },
        "twilio": {
            "account_sid":    t(rc.twilio_account_sid),
            "auth_token":     t(rc.twilio_auth_token),
            "api_key_sid":    t(rc.twilio_api_key_sid),
            "api_key_secret": t(rc.twilio_api_key_secret),
            "twiml_app_sid":  t(rc.twilio_twiml_app_sid),
            "minimally_configured": bool((rc.twilio_account_sid or "").strip() and (rc.twilio_auth_token or "").strip()),
            "voice_sdk_ready": all([(rc.twilio_account_sid or "").strip(),
                                    (rc.twilio_auth_token or "").strip(),
                                    (rc.twilio_api_key_sid or "").strip(),
                                    (rc.twilio_api_key_secret or "").strip(),
                                    (rc.twilio_twiml_app_sid or "").strip()]),
        },
        "deepgram": {
            "set": bool((rc.deepgram_api_key or "").strip()),
            "masked": mask_key(rc.deepgram_api_key),
        },
        "blooio": {
            "set": bool((rc.blooio_api_key or "").strip()),
            "masked": mask_key(rc.blooio_api_key),
            "signing_secret_set": bool((rc.blooio_signing_secret or "").strip()),
            "signing_secret_masked": mask_key(rc.blooio_signing_secret),
        },
        "resend": {
            "webhook_secret_db_set": bool((rc.resend_webhook_secret or "").strip()),
            "webhook_secret_env_set": bool((settings_obj.resend_webhook_secret or "").strip()),
            "webhook_secret_source":
                "database" if (rc.resend_webhook_secret or "").strip()
                else ("env" if (settings_obj.resend_webhook_secret or "").strip() else "none"),
            "webhook_secret_masked": mask_key((rc.resend_webhook_secret or "").strip() or settings_obj.resend_webhook_secret),
        },
        "google_maps": {
            "set": bool((rc.google_maps_api_key or "").strip() or (settings_obj.google_maps_api_key or "").strip()),
            "source":
                "database" if (rc.google_maps_api_key or "").strip()
                else ("env" if (settings_obj.google_maps_api_key or "").strip() else "none"),
            "masked": mask_key((rc.google_maps_api_key or "").strip() or settings_obj.google_maps_api_key),
        },
    }


def _payload(rc, settings_obj, *, include_platform: bool) -> dict:
    """Combined payload. Admins get tenant-tier only; super_admins get the lot."""
    out = _tenant_payload(rc, settings_obj)
    out["tier"] = "platform" if include_platform else "tenant"
    if include_platform:
        out.update(_platform_payload(rc, settings_obj))
    return out


@router.get("/brand")
async def get_brand(
    db: AsyncSession = Depends(get_tenant_db),
):
    """Public org brand — colors + logo + company name. No auth so the
    login page + public booking pages can pick up the brand too. This
    is the same data exposed inside /api/runtime-config's `brand` key,
    just without the admin gate. It's deliberately a small, public-by-
    design surface — nothing sensitive lives here."""
    return await get_org_brand(db)


@router.get("/runtime-config")
async def get_runtime_config(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Returns config visible to the requesting user.
       - super_admin: full payload (tenant + platform)
       - admin:       tenant-tier only (no Twilio / Deepgram / Blooio / webhooks)
       - everyone else: 403
    """
    from app.config import settings
    if user.role not in ("admin", "super_admin"):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Insufficient privilege")
    rc = await _get_or_create(db)
    return _payload(rc, settings, include_platform=(user.role == "super_admin"))


@router.get("/runtime-config/native-scheduler-hosts")
async def list_native_scheduler_hosts(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Used by the Audit Reports settings page to populate the
    'pick which rep's booking page' dropdown when scheduler_type
    is 'native'. Lists active users who have connected Google
    Calendar and have a booking_slug assigned."""
    if user.role not in ("admin", "super_admin"):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Admin access required")
    from sqlalchemy import select
    rows = (await db.execute(
        select(User).where(
            User.is_active == True,
            User.google_refresh_token.isnot(None),
            User.booking_slug.isnot(None),
        ).order_by(User.first_name, User.last_name)
    )).scalars().all()
    return [
        {
            "id": u.id,
            "name": u.full_name or u.email,
            "email": u.email,
            "booking_slug": u.booking_slug,
        }
        for u in rows
    ]


@router.get("/runtime-config/messaging-default")
async def get_messaging_default(
    user: User = Depends(get_current_user),
):
    """Returns the full in-code default messaging direction text — used by
    the Settings UI's 'Load Default' button so the user can see / edit it
    before saving as a custom direction."""
    return {"text": DEFAULT_MESSAGING_DIRECTION}


@router.patch("/runtime-config")
async def update_runtime_config(
    req: UpdateRuntimeConfigRequest,
    request: Request,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Update runtime config. Per-field role gating:
       - tenant-tier (admin + super_admin): apollo_api_key,
         messaging_direction, brand_*, audit_branding, pipeline_stages
       - platform-tier (super_admin only): netrows, twilio_*, deepgram,
         blooio, resend_webhook_secret, google_maps

    Integration credentials (Twilio, Netrows, Blooio, etc.) are managed
    centrally by the platform admin from /admin's API Keys vault, not
    by tenant admins in their own Settings UI. Phone-number purchases +
    voice/SMS sending still work for tenant admins via the routes that
    read the credentials internally; the creds are just not visible to
    them.
    """
    from fastapi import HTTPException
    if user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Insufficient privilege")

    is_super = (user.role == "super_admin")

    # Platform-tier fields rejected for admins.
    platform_changes = (
        req.netrows_api_key,
        req.twilio_account_sid, req.twilio_auth_token,
        req.twilio_api_key_sid, req.twilio_api_key_secret, req.twilio_twiml_app_sid,
        req.deepgram_api_key,
        req.blooio_api_key, req.blooio_signing_secret,
        req.resend_webhook_secret,
        req.google_maps_api_key,
    )
    if any(v is not None for v in platform_changes) and not is_super:
        raise HTTPException(status_code=403, detail="Only super admins can modify platform credentials")

    # Tenant-tier writes (admin + super_admin):
    #   - apollo_api_key (the one BYO integration in the SaaS model)
    #   - messaging_direction (team voice / AI tone)
    if req.apollo_api_key is not None:
        await set_apollo_api_key(db, req.apollo_api_key)
    if req.messaging_direction is not None:
        await set_messaging_direction(db, req.messaging_direction)
    if req.imessage_enabled is not None:
        # NB: do NOT `import _get_or_create` here — it's already imported at
        # module scope. A local import makes the name function-local for the
        # WHOLE function, so when this branch is skipped (e.g. a brand-only
        # save) the later `_get_or_create(db)` call hits UnboundLocalError.
        rc = await _get_or_create(db)
        rc.imessage_enabled = bool(req.imessage_enabled)
        await db.flush()
    # Org brand — tenant-tier, admin can manage
    _brand_fields = (
        req.brand_primary_color, req.brand_secondary_color,
        req.brand_accent_bg_color, req.brand_logo_url, req.brand_company_name,
        req.brand_website_url,
    )
    if any(v is not None for v in _brand_fields):
        await set_org_brand(
            db,
            primary_color=req.brand_primary_color,
            secondary_color=req.brand_secondary_color,
            accent_bg_color=req.brand_accent_bg_color,
            logo_url=req.brand_logo_url,
            company_name=req.brand_company_name,
            website_url=req.brand_website_url,
        )

    _audit_fields = (
        req.audit_report_header_url, req.audit_report_logo_url,
        req.audit_left_image_url, req.audit_left_message,
        req.audit_right_image_url, req.audit_right_message,
        req.audit_scheduler_type, req.audit_native_user_id, req.audit_custom_url,
    )
    if any(v is not None for v in _audit_fields):
        await set_audit_branding(
            db,
            header_url=req.audit_report_header_url,
            logo_url=req.audit_report_logo_url,
            left_image_url=req.audit_left_image_url,
            left_message=req.audit_left_message,
            right_image_url=req.audit_right_image_url,
            right_message=req.audit_right_message,
            scheduler_type=req.audit_scheduler_type,
            native_user_id=req.audit_native_user_id,
            custom_url=req.audit_custom_url,
        )

    # Platform-tier writes (super_admin only)
    if is_super:
        if req.netrows_api_key is not None:
            await set_netrows_api_key(db, req.netrows_api_key)
        twilio_changes = {
            "account_sid":    req.twilio_account_sid,
            "auth_token":     req.twilio_auth_token,
            "api_key_sid":    req.twilio_api_key_sid,
            "api_key_secret": req.twilio_api_key_secret,
            "twiml_app_sid":  req.twilio_twiml_app_sid,
        }
        if any(v is not None for v in twilio_changes.values()):
            await set_twilio_credentials(db, **twilio_changes)
        if req.deepgram_api_key is not None:
            await set_deepgram_api_key(db, req.deepgram_api_key)
        if req.blooio_api_key is not None:
            await set_blooio_api_key(db, req.blooio_api_key)
        if req.blooio_signing_secret is not None:
            await set_blooio_signing_secret(db, req.blooio_signing_secret)
        if req.resend_webhook_secret is not None:
            await set_resend_webhook_secret(db, req.resend_webhook_secret)
        if req.google_maps_api_key is not None:
            await set_google_maps_api_key(db, req.google_maps_api_key)

    # Audit summary — record which fields were touched, never the values
    touched_fields = []
    for field_name in (
        "netrows_api_key", "twilio_account_sid", "twilio_auth_token",
        "twilio_api_key_sid", "twilio_api_key_secret", "twilio_twiml_app_sid",
        "deepgram_api_key", "blooio_api_key", "blooio_signing_secret",
        "resend_webhook_secret", "messaging_direction", "apollo_api_key",
        "google_maps_api_key",
        "audit_report_header_url", "audit_report_logo_url",
        "audit_left_image_url", "audit_left_message",
        "audit_right_image_url", "audit_right_message",
        "audit_scheduler_type", "audit_native_user_id", "audit_custom_url",
        "brand_primary_color", "brand_secondary_color", "brand_accent_bg_color",
        "brand_logo_url", "brand_company_name",
    ):
        val = getattr(req, field_name)
        if val is not None:
            touched_fields.append({
                "field": field_name,
                "set": bool(val.strip()) if isinstance(val, str) else bool(val),
            })
    if touched_fields:
        await record_audit(
            db, actor=user, action="runtime_config.updated",
            target_type="runtime_config", target_id=1, target_label="org config",
            metadata={"changes": touched_fields}, request=request,
        )

    from app.config import settings
    rc = await _get_or_create(db)
    await db.commit()
    return _payload(rc, settings, include_platform=is_super)


# ======================================================================
# Tenant self-service: Email & Domains (Settings → Email & Domains)
# ----------------------------------------------------------------------
# Tenant ADMINS manage their own sending domain (Resend) + custom app
# domain here — no super-admin / /admin console needed. Everything is
# scoped to the caller's resolved tenant (db.info["tenant_id"]), never a
# path param, so one tenant can't touch another's domains.
# ======================================================================

# The server's public IP — a custom app domain must A-record here before
# Caddy will issue its TLS cert (see /api/admin/caddy/ask).
APP_DOMAIN_TARGET_IP = "72.62.168.160"


def _require_admin(user: User) -> None:
    from fastapi import HTTPException
    if user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")


def _tenant_id(db: AsyncSession) -> int:
    from fastapi import HTTPException
    tid = db.info.get("tenant_id")
    if not tid:
        raise HTTPException(status_code=400, detail="No tenant in context")
    return int(tid)


class SendingDomainIn(BaseModel):
    domain_name: str = ""


@router.get("/runtime-config/domains")
async def get_tenant_domains(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Sending domain (Resend) + app domains for the caller's tenant."""
    _require_admin(user)
    import json as _json
    from sqlalchemy import select
    from app.models import RuntimeConfig, TenantDomain
    from app.routes.admin_routes import _format_dns_records
    tid = _tenant_id(db)

    rc = (await db.execute(select(RuntimeConfig).where(RuntimeConfig.tenant_id == tid))).scalar_one_or_none()
    sending = {"configured": False}
    if rc and rc.resend_domain_id:
        recs = _json.loads(rc.resend_domain_records_json) if rc.resend_domain_records_json else []
        sending = {
            "configured": True,
            "domain_name": rc.resend_domain_name,
            "status": rc.resend_domain_status,
            "records": _format_dns_records(recs),
        }

    rows = (await db.execute(
        select(TenantDomain).where(TenantDomain.tenant_id == tid).order_by(TenantDomain.is_primary.desc(), TenantDomain.id)
    )).scalars().all()
    app_domains = [{
        "domain": d.domain,
        "is_primary": bool(d.is_primary),
        "is_verified": bool(d.is_verified),
        # leadprospector.ai subdomains are platform-managed; custom domains
        # are the ones a tenant adds and must point DNS at us.
        "is_platform": d.domain.endswith(".leadprospector.ai"),
    } for d in rows]

    return {
        "sending": sending,
        "app_domains": app_domains,
        "app_domain_target_ip": APP_DOMAIN_TARGET_IP,
    }


@router.post("/runtime-config/sending-domain")
async def set_tenant_sending_domain(
    req: SendingDomainIn,
    request: Request,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Register the tenant's sending domain in Resend + store the DNS records
    they need to add. Idempotent-ish: re-registering an existing domain
    returns its current records/status."""
    _require_admin(user)
    import json as _json
    from sqlalchemy import select
    from app.models import RuntimeConfig
    from app.routes.admin_routes import _format_dns_records
    from app.services.resend_provisioning import register_domain, get_domain_status, is_configured
    from fastapi import HTTPException
    tid = _tenant_id(db)

    if not is_configured():
        raise HTTPException(status_code=400, detail="Email sending is not configured on the platform yet")
    name = (req.domain_name or "").strip().lower().rstrip(".")
    if not name or "." not in name:
        raise HTTPException(status_code=400, detail="Enter a valid domain, e.g. go.yourcompany.com")

    result = await register_domain(name)
    if not result:
        # Already in Resend (register returns 4xx) — look it up by listing.
        raise HTTPException(status_code=502, detail=f"Could not register '{name}'. If it already exists in Resend, it's likely on a different account — contact support.")

    rc = (await db.execute(select(RuntimeConfig).where(RuntimeConfig.tenant_id == tid))).scalar_one_or_none()
    if not rc:
        rc = RuntimeConfig(tenant_id=tid)
        db.add(rc)
    rc.resend_domain_id = result["domain_id"]
    rc.resend_domain_name = result["domain_name"]
    rc.resend_domain_records_json = _json.dumps(result["records"])
    rc.resend_domain_status = result["status"]
    await db.commit()
    await record_audit(db, actor=user, action="sending_domain_set",
                       target_type="tenant", target_id=tid,
                       metadata={"domain": result["domain_name"]}, request=request)
    return {
        "domain_name": result["domain_name"],
        "status": rc.resend_domain_status,
        "records": _format_dns_records(result["records"]),
    }


@router.post("/runtime-config/sending-domain/verify")
async def verify_tenant_sending_domain(
    request: Request,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Ask Resend to re-check DNS, then refresh stored status + records."""
    _require_admin(user)
    import json as _json
    from sqlalchemy import select
    from app.models import RuntimeConfig
    from app.routes.admin_routes import _format_dns_records
    from app.services.resend_provisioning import trigger_verify, get_domain_status
    from fastapi import HTTPException
    tid = _tenant_id(db)

    rc = (await db.execute(select(RuntimeConfig).where(RuntimeConfig.tenant_id == tid))).scalar_one_or_none()
    if not rc or not rc.resend_domain_id:
        raise HTTPException(status_code=404, detail="No sending domain set yet")
    await trigger_verify(rc.resend_domain_id)
    status_data = await get_domain_status(rc.resend_domain_id)
    if not status_data:
        raise HTTPException(status_code=502, detail="Could not reach Resend to check status")
    rc.resend_domain_status = status_data.get("status") or rc.resend_domain_status
    recs = status_data.get("records") or []
    if recs:
        rc.resend_domain_records_json = _json.dumps(recs)
    await db.commit()
    return {
        "domain_name": rc.resend_domain_name,
        "status": rc.resend_domain_status,
        "records": _format_dns_records(recs or (_json.loads(rc.resend_domain_records_json) if rc.resend_domain_records_json else [])),
    }


class AppDomainIn(BaseModel):
    domain: str = ""


@router.post("/runtime-config/app-domain")
async def add_tenant_app_domain(
    req: AppDomainIn,
    request: Request,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Register a custom app domain (e.g. prospector.yourcompany.com) for
    the caller's tenant. Stored unverified until DNS points at us; the
    response tells them the A record to add."""
    _require_admin(user)
    import re as _re
    from sqlalchemy import select
    from app.models import TenantDomain
    from fastapi import HTTPException
    tid = _tenant_id(db)

    d = (req.domain or "").strip().lower().rstrip(".")
    if not _re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", d):
        raise HTTPException(status_code=400, detail="Enter a valid hostname, e.g. prospector.yourcompany.com")
    existing = (await db.execute(select(TenantDomain).where(TenantDomain.domain == d))).scalar_one_or_none()
    if existing:
        if existing.tenant_id != tid:
            raise HTTPException(status_code=409, detail="That domain is already in use")
        return {"domain": d, "is_verified": bool(existing.is_verified), "target_ip": APP_DOMAIN_TARGET_IP}
    db.add(TenantDomain(tenant_id=tid, domain=d, is_primary=False, is_verified=False))
    await db.commit()
    await record_audit(db, actor=user, action="app_domain_added",
                       target_type="tenant", target_id=tid, metadata={"domain": d}, request=request)
    return {"domain": d, "is_verified": False, "target_ip": APP_DOMAIN_TARGET_IP}


@router.post("/runtime-config/app-domain/verify")
async def verify_tenant_app_domain(
    req: AppDomainIn,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Check the custom domain's A record points at us; if so mark it
    verified so Caddy will issue its TLS cert on first hit."""
    _require_admin(user)
    import socket
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.models import TenantDomain
    from fastapi import HTTPException
    tid = _tenant_id(db)

    d = (req.domain or "").strip().lower().rstrip(".")
    row = (await db.execute(
        select(TenantDomain).where(TenantDomain.domain == d, TenantDomain.tenant_id == tid)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Domain not found for this tenant")
    try:
        _, _, ips = socket.gethostbyname_ex(d)
    except Exception:
        ips = []
    points_here = APP_DOMAIN_TARGET_IP in ips
    if points_here and not row.is_verified:
        row.is_verified = True
        row.verified_at = datetime.now(timezone.utc)
        await db.commit()
    return {
        "domain": d,
        "is_verified": bool(row.is_verified),
        "points_here": points_here,
        "resolved_ips": ips,
        "target_ip": APP_DOMAIN_TARGET_IP,
    }
