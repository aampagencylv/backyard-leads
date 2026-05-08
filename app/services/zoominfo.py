"""
ZoomInfo integration — Public API + PKI authentication.

Auth flow (ZoomInfo's PKI / signed-JWT pattern):
  1. Tenant registers an app in ZoomInfo developer portal, gets:
       - username (their account email)
       - client_id (app's identifier)
       - private_key (RSA PEM)
  2. We sign a short-lived JWT (5 min) with claims that identify the
     account + client + sign with the private key (RS256).
  3. POST that signed JWT to /authenticate → returns an access token
     valid for ~24h.
  4. Cache the access token in runtime_config until expiry; refresh
     when it's about to lapse or a 401 comes back.
  5. Use access token in `Authorization: Bearer <token>` for every
     /enrich/* and /search/* call.

Endpoints we wrap (v1):
  POST /enrich/contact   — enrich a contact by email or LinkedIn
  POST /enrich/company   — enrich a company by domain
  POST /search/contact   — find contacts by criteria (title, company, geo)
  POST /search/company   — find companies by criteria

Cost model: BYO-key — tenant pays ZoomInfo per record. We charge a
small orchestration fee in credits (see credit_meter rate-card
'enrich_zoominfo'). Slot in the EnrichmentWaterfall as the FIRST
provider since ZoomInfo's data quality > Apollo > Netrows for B2B
SMB-and-up coverage.

Compliance:
- ZoomInfo data is licensed B2B contact data — they handle GDPR/CCPA
  on their end. We just relay results into the CRM.
- Private key stored unencrypted at rest in runtime_config; OK for v1
  single-tenant. SaaS multi-tenant deploys should encrypt with a
  per-tenant KMS key (queued for SaaS billing-layer work).
"""
from __future__ import annotations
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RuntimeConfig

log = logging.getLogger("bmp.zoominfo")

ZOOMINFO_BASE = "https://api.zoominfo.com"
TOKEN_REFRESH_BUFFER = timedelta(minutes=15)  # refresh when within 15 min of expiry


# ============================================================
# Result shapes
# ============================================================

@dataclass
class ZoomInfoContact:
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    job_title: Optional[str] = None
    company_name: Optional[str] = None
    company_domain: Optional[str] = None
    phone: Optional[str] = None
    direct_phone: Optional[str] = None
    mobile_phone: Optional[str] = None
    linkedin_url: Optional[str] = None
    location: Optional[str] = None
    department: Optional[str] = None
    seniority: Optional[str] = None
    raw: Optional[dict] = None


@dataclass
class ZoomInfoCompany:
    name: Optional[str] = None
    domain: Optional[str] = None
    industry: Optional[str] = None
    sub_industry: Optional[str] = None
    employee_count: Optional[int] = None
    employee_range: Optional[str] = None       # e.g. "10-50"
    revenue_range: Optional[str] = None        # e.g. "$1M-$10M"
    revenue: Optional[float] = None             # exact when ZoomInfo has it
    founded: Optional[str] = None
    headquarters: Optional[str] = None
    phone: Optional[str] = None
    linkedin_url: Optional[str] = None
    technologies: list[str] = field(default_factory=list)
    description: Optional[str] = None
    raw: Optional[dict] = None


# ============================================================
# Credentials helpers
# ============================================================

async def _load_creds(db: AsyncSession) -> Optional[RuntimeConfig]:
    rc = (await db.execute(select(RuntimeConfig).where(RuntimeConfig.id == 1))).scalar_one_or_none()
    if not rc:
        return None
    if not (rc.zoominfo_username or "").strip():
        return None
    if not (rc.zoominfo_client_id or "").strip():
        return None
    if not (rc.zoominfo_private_key or "").strip():
        return None
    return rc


def _is_token_fresh(rc: RuntimeConfig) -> bool:
    if not rc.zoominfo_access_token:
        return False
    if not rc.zoominfo_token_expires_at:
        return False
    expires = rc.zoominfo_token_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) + TOKEN_REFRESH_BUFFER < expires


# ============================================================
# JWT mint + token exchange
# ============================================================

def _sign_pki_jwt(username: str, client_id: str, private_key_pem: str) -> str:
    """Sign a short-lived JWT identifying the tenant + app for the
    /authenticate exchange. RS256 algorithm; 5-minute lifetime.

    ZoomInfo expects:
      iss / sub      = username (the account email)
      username       = same
      client_id      = the app's client_id
      iat / exp      = standard issued/expires timestamps
    """
    from jose import jwt  # python-jose is already in requirements
    now = int(time.time())
    claims = {
        "iss": username,
        "sub": username,
        "username": username,
        "client_id": client_id,
        "iat": now,
        "exp": now + 300,
        "aud": "enterprise_api",
    }
    return jwt.encode(claims, private_key_pem, algorithm="RS256")


async def _exchange_for_access_token(
    db: AsyncSession,
    rc: RuntimeConfig,
) -> Optional[str]:
    """Mint a signed JWT, exchange at /authenticate for an access token,
    cache it on the runtime_config row. Returns the access token or None
    on auth failure."""
    try:
        signed = _sign_pki_jwt(
            rc.zoominfo_username.strip(),
            rc.zoominfo_client_id.strip(),
            rc.zoominfo_private_key.strip(),
        )
    except Exception as e:
        log.warning(f"ZoomInfo JWT signing failed: {e}")
        return None

    body = {
        "client_id": rc.zoominfo_client_id.strip(),
        "username": rc.zoominfo_username.strip(),
        "jwt": signed,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{ZOOMINFO_BASE}/authenticate",
                headers={"Content-Type": "application/json"},
                json=body,
            )
    except httpx.HTTPError as e:
        log.warning(f"ZoomInfo /authenticate network error: {e}")
        return None

    if r.status_code != 200:
        log.warning(f"ZoomInfo /authenticate {r.status_code}: {r.text[:300]}")
        return None
    try:
        data = r.json() or {}
    except Exception:
        return None

    token = data.get("jwt") or data.get("access_token") or data.get("token")
    expires_in = data.get("expires_in") or data.get("expires") or (24 * 3600)
    if not token:
        log.warning(f"ZoomInfo /authenticate returned no token field: {list(data.keys())}")
        return None
    try:
        expires_in = int(expires_in)
    except (ValueError, TypeError):
        expires_in = 24 * 3600

    rc.zoominfo_access_token = token
    rc.zoominfo_token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    try:
        await db.commit()
    except Exception:
        pass
    return token


async def _get_access_token(db: AsyncSession) -> Optional[str]:
    """Return a valid access token. Loads creds, returns cached token
    when fresh, otherwise mints a new one."""
    rc = await _load_creds(db)
    if rc is None:
        return None
    if _is_token_fresh(rc):
        return rc.zoominfo_access_token
    return await _exchange_for_access_token(db, rc)


# ============================================================
# Public helpers
# ============================================================

async def is_configured(db: AsyncSession) -> bool:
    """Returns True when ZoomInfo PKI credentials are present + valid."""
    rc = await _load_creds(db)
    return rc is not None


async def test_connection(db: AsyncSession) -> dict:
    """Test the auth flow end-to-end. Used by Settings UI's 'Test'
    button to confirm credentials are correct before saving."""
    rc = await _load_creds(db)
    if rc is None:
        return {"ok": False, "error": "credentials not configured"}
    token = await _get_access_token(db)
    if not token:
        return {"ok": False, "error": "auth_failed — check username, client_id, and private key"}
    masked = f"{token[:10]}...{token[-6:]}" if len(token) > 20 else "<short>"
    return {
        "ok": True,
        "token_masked": masked,
        "expires_at": rc.zoominfo_token_expires_at.isoformat() if rc.zoominfo_token_expires_at else None,
    }


# ============================================================
# Enrich endpoints
# ============================================================

async def enrich_company(db: AsyncSession, domain: str) -> Optional[ZoomInfoCompany]:
    """Look up a company in ZoomInfo by domain. Returns None when not
    configured / not found / API error."""
    if not domain:
        return None
    token = await _get_access_token(db)
    if not token:
        return None

    clean = (domain or "").replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{ZOOMINFO_BASE}/enrich/company",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"matchCompanyInput": [{"companyWebsite": clean}],
                      "outputFields": [
                          "id", "name", "website", "industry", "subIndustry",
                          "employeeCount", "employeeRange", "revenue", "revenueRange",
                          "foundedYear", "phone", "ziCompanyLocations",
                          "linkedInUrl", "techStack", "description",
                      ]},
            )
    except httpx.HTTPError as e:
        log.warning(f"ZoomInfo enrich_company network error for {clean}: {e}")
        return None

    if r.status_code == 401:
        # Token might have expired between cache check + use; force refresh + retry once
        log.info("ZoomInfo 401 — forcing token refresh + retry")
        rc = await _load_creds(db)
        if rc is None:
            return None
        rc.zoominfo_token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        return await enrich_company(db, domain)

    if r.status_code != 200:
        log.warning(f"ZoomInfo enrich_company {r.status_code}: {r.text[:300]}")
        return None

    try:
        data = r.json() or {}
    except Exception:
        return None
    items = data.get("data") or data.get("results") or []
    if not items:
        return None
    body = items[0] if isinstance(items, list) else items

    techs = body.get("techStack") or body.get("technologies") or []
    if isinstance(techs, list):
        tech_names = [t.get("name") if isinstance(t, dict) else str(t) for t in techs]
    else:
        tech_names = []

    return ZoomInfoCompany(
        name=body.get("name"),
        domain=body.get("website") or clean,
        industry=body.get("industry"),
        sub_industry=body.get("subIndustry"),
        employee_count=int(body["employeeCount"]) if body.get("employeeCount") else None,
        employee_range=body.get("employeeRange"),
        revenue_range=body.get("revenueRange"),
        revenue=float(body["revenue"]) if body.get("revenue") else None,
        founded=str(body.get("foundedYear")) if body.get("foundedYear") else None,
        phone=body.get("phone"),
        linkedin_url=body.get("linkedInUrl"),
        technologies=[t for t in tech_names if t][:30],
        description=(body.get("description") or "")[:1000] or None,
        headquarters=_format_location(body.get("ziCompanyLocations")),
        raw=body if isinstance(body, dict) else None,
    )


def _format_location(locations: Any) -> Optional[str]:
    if not locations:
        return None
    if isinstance(locations, list) and locations:
        loc = locations[0]
    elif isinstance(locations, dict):
        loc = locations
    else:
        return None
    parts = [loc.get(k) for k in ("city", "state", "country") if loc.get(k)]
    return ", ".join(parts) if parts else None


async def enrich_contact_by_email(db: AsyncSession, email: str) -> Optional[ZoomInfoContact]:
    """Look up a contact in ZoomInfo by email. Direct phone + mobile
    are the most valuable fields (Apollo/Netrows often miss these)."""
    if not email or "@" not in email:
        return None
    token = await _get_access_token(db)
    if not token:
        return None

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{ZOOMINFO_BASE}/enrich/contact",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "matchPersonInput": [{"emailAddress": email.strip().lower()}],
                    "outputFields": [
                        "id", "firstName", "lastName", "email",
                        "jobTitle", "department", "seniority",
                        "directPhoneNumber", "mobilePhone", "phone",
                        "linkedInUrl",
                        "company", "companyWebsite",
                    ],
                },
            )
    except httpx.HTTPError as e:
        log.warning(f"ZoomInfo enrich_contact network error for {email}: {e}")
        return None

    if r.status_code == 401:
        rc = await _load_creds(db)
        if rc is None:
            return None
        rc.zoominfo_token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        return await enrich_contact_by_email(db, email)

    if r.status_code != 200:
        log.warning(f"ZoomInfo enrich_contact {r.status_code}: {r.text[:200]}")
        return None

    try:
        data = r.json() or {}
    except Exception:
        return None
    items = data.get("data") or data.get("results") or []
    if not items:
        return None
    body = items[0] if isinstance(items, list) else items

    return ZoomInfoContact(
        email=body.get("email") or email,
        first_name=body.get("firstName"),
        last_name=body.get("lastName"),
        full_name=f"{body.get('firstName', '')} {body.get('lastName', '')}".strip() or None,
        job_title=body.get("jobTitle"),
        company_name=(body.get("company") or {}).get("name") if isinstance(body.get("company"), dict) else body.get("company"),
        company_domain=body.get("companyWebsite"),
        phone=body.get("phone"),
        direct_phone=body.get("directPhoneNumber"),
        mobile_phone=body.get("mobilePhone"),
        linkedin_url=body.get("linkedInUrl"),
        department=body.get("department"),
        seniority=body.get("seniority"),
        raw=body if isinstance(body, dict) else None,
    )


async def search_contacts_at_company(
    db: AsyncSession,
    company_domain: str,
    *,
    titles: Optional[list[str]] = None,
    limit: int = 10,
) -> list[ZoomInfoContact]:
    """Find people at a company by domain + optional title filter.
    Used by the EnrichmentWaterfall ZoomInfoProvider to pull
    decision-makers when we have a company but no contacts yet."""
    if not company_domain:
        return []
    token = await _get_access_token(db)
    if not token:
        return []

    clean = (company_domain or "").replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")
    payload = {
        "companyDomain": clean,
        "rpp": min(max(limit, 1), 25),
        "outputFields": [
            "firstName", "lastName", "email", "jobTitle",
            "directPhoneNumber", "mobilePhone", "linkedInUrl",
            "department", "seniority", "company",
        ],
    }
    if titles:
        payload["jobTitle"] = titles

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{ZOOMINFO_BASE}/search/contact",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as e:
        log.warning(f"ZoomInfo search_contact network error: {e}")
        return []

    if r.status_code == 401:
        rc = await _load_creds(db)
        if rc is None:
            return []
        rc.zoominfo_token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        return await search_contacts_at_company(db, company_domain, titles=titles, limit=limit)

    if r.status_code != 200:
        log.warning(f"ZoomInfo search_contact {r.status_code}: {r.text[:200]}")
        return []

    try:
        data = r.json() or {}
    except Exception:
        return []
    items = data.get("data") or data.get("results") or []

    results: list[ZoomInfoContact] = []
    for body in items[:limit]:
        results.append(ZoomInfoContact(
            email=body.get("email"),
            first_name=body.get("firstName"),
            last_name=body.get("lastName"),
            full_name=f"{body.get('firstName', '')} {body.get('lastName', '')}".strip() or None,
            job_title=body.get("jobTitle"),
            company_name=(body.get("company") or {}).get("name") if isinstance(body.get("company"), dict) else body.get("company"),
            company_domain=clean,
            direct_phone=body.get("directPhoneNumber"),
            mobile_phone=body.get("mobilePhone"),
            linkedin_url=body.get("linkedInUrl"),
            department=body.get("department"),
            seniority=body.get("seniority"),
            raw=body if isinstance(body, dict) else None,
        ))
    return results
