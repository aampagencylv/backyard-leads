"""
Enrichment waterfall — orchestrates contact discovery across providers.

Architecture: each provider is a class implementing the EnrichmentProvider
Protocol. The Waterfall runs them in priority order, deduping contacts by
email across the cascade. Result is a unified list with provenance
(which provider found which contact) so the UI / COGS dashboard can show
where each lead came from.

Provider order (hardcoded for v1 — per-tenant config later):
  1. Apollo  — only when tenant has BYO key. Best for B2B/SaaS verticals;
              direct mobile numbers on many records.
  2. Netrows — platform-paid. Broad SMB coverage; decision-maker focus.
  3. Hunter  — platform-paid email finder. Last resort, low cost.

Cost model:
  - Apollo: per-record cost stays on the tenant's Apollo bill. We charge
            ~2 credits per call as an orchestration fee.
  - Netrows / Hunter: platform pays vendor cost. We charge full credit
            rate (10 / 8 credits) which sets margin in the SaaS plan.

Each provider's spend is metered separately as enrich_<vendor> so the
admin COGS dashboard breaks down by source.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Protocol
import httpx
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.runtime_config import get_netrows_api_key, get_apollo_api_key
from app.services.netrows_enrichment import find_decision_makers as netrows_find_decision_makers
from app.services.hunter_enrichment import search_domain as hunter_search

log = logging.getLogger("bmp.enrichment")


# ============================================================
# Result shapes
# ============================================================

@dataclass
class WaterfallContact:
    """One contact candidate returned by a provider. Multiple providers
    may return the same email — the waterfall dedupes; first writer wins
    on the merged record but later providers can fill in missing fields."""
    email: str
    full_name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    job_title: Optional[str] = None
    phone: Optional[str] = None         # Generic — landline or unknown
    mobile_phone: Optional[str] = None  # Apollo direct dial / verified mobile
    linkedin_url: Optional[str] = None
    email_status: str = "unknown"        # 'verified' | 'valid' | 'unknown' | 'invalid'
    source: str = ""                     # 'apollo' | 'netrows_dm' | 'hunter'
    confidence: int = 0                  # 0-100, provider-reported


@dataclass
class WaterfallResult:
    contacts: list[WaterfallContact] = field(default_factory=list)
    # Order matters here — earliest provider's output appears first
    providers_called: list[str] = field(default_factory=list)
    # Provider name → error message string. Empty when everything worked.
    errors: dict[str, str] = field(default_factory=dict)
    # Firmographic data: first non-null value across providers wins.
    # Keys are stable: employee_count, industry, linkedin_url, founded, etc.
    company_data: dict = field(default_factory=dict)


# ============================================================
# Provider protocol
# ============================================================

class EnrichmentProvider(Protocol):
    """All providers implement this interface. Each is responsible for its
    own API call, error handling, and metering. The waterfall just
    orchestrates the cascade."""

    name: str  # 'apollo' | 'netrows_dm' | 'hunter'

    async def is_available(self, db: AsyncSession) -> bool:
        """Check whether this provider has the credentials it needs to run.
        Skipped silently when False."""
        ...

    async def enrich(
        self, db: AsyncSession, *, domain: str, company_name: str = "",
    ) -> tuple[list[WaterfallContact], dict, Optional[str]]:
        """Run the lookup for one company.

        Returns (contacts, company_data_patch, error_message_or_None).
        Implementations must:
          - Never raise. Catch exceptions and return ([], {}, str(e)).
          - Meter their own credit_meter row using the right action_type.
        """
        ...


# ============================================================
# ZoomInfo provider — premium B2B data, BYO credentials
# ============================================================

class ZoomInfoProvider:
    """ZoomInfo's data quality > Apollo > Netrows for SMB-and-up B2B
    coverage. Especially strong on direct-dial mobile numbers — the
    single highest-value field for SMS + voice outreach.

    Auth is PKI/JWT (see app/services/zoominfo.py); BYO credentials.
    Slots first in the waterfall when configured."""

    name = "zoominfo"

    async def is_available(self, db: AsyncSession) -> bool:
        from app.services.zoominfo import is_configured
        return await is_configured(db)

    async def enrich(self, db, *, domain, company_name=""):
        from app.services.zoominfo import (
            enrich_company as zi_enrich_company,
            search_contacts_at_company,
        )
        if not domain:
            return [], {}, "no_domain"

        contacts: list[WaterfallContact] = []
        company_patch: dict = {}
        errors_acc: list[str] = []

        # 1. Company-level enrichment for firmographics
        try:
            zi_company = await zi_enrich_company(db, domain)
            if zi_company:
                if zi_company.employee_count:
                    company_patch["employee_count"] = zi_company.employee_count
                if zi_company.industry:
                    company_patch["industry"] = zi_company.industry
                if zi_company.revenue_range:
                    company_patch["revenue_range"] = zi_company.revenue_range
                if zi_company.linkedin_url:
                    company_patch["linkedin_url"] = zi_company.linkedin_url
                if zi_company.founded:
                    company_patch["founded"] = zi_company.founded
                if zi_company.description:
                    company_patch["description"] = zi_company.description
        except Exception as e:
            errors_acc.append(f"company: {e}")

        # 2. Contact search at this company — pull decision-makers
        try:
            people = await search_contacts_at_company(
                db, domain,
                titles=["CEO", "Founder", "Owner", "President",
                         "VP Sales", "VP Marketing", "Director", "Head of"],
                limit=10,
            )
            for p in people:
                email = (p.email or "").strip().lower()
                if not email or "@" not in email:
                    continue
                contacts.append(WaterfallContact(
                    email=email,
                    full_name=p.full_name,
                    first_name=p.first_name,
                    last_name=p.last_name,
                    job_title=p.job_title,
                    phone=p.direct_phone or p.phone,
                    mobile_phone=p.mobile_phone,
                    linkedin_url=p.linkedin_url,
                    email_status="valid",  # ZoomInfo verifies emails
                    source="zoominfo",
                    confidence=95,  # highest tier when ZoomInfo returns a hit
                ))
        except Exception as e:
            errors_acc.append(f"search: {e}")

        # Meter at the route level — see company_routes.py wiring; the
        # provider class doesn't double-meter here.
        err = "; ".join(errors_acc) if errors_acc and not contacts and not company_patch else None
        return contacts, company_patch, err


# ============================================================
# Apollo provider — the one customer-supplied integration
# ============================================================

APOLLO_BASE = "https://api.apollo.io/api/v1"
APOLLO_TARGET_TITLES = (
    "CEO", "Founder", "Co-Founder", "Owner", "President", "Managing Director",
    "VP", "Director", "Head of Marketing", "Head of Growth",
    "Marketing Director", "VP Marketing", "VP Growth", "Chief Marketing Officer",
)


class ApolloProvider:
    name = "apollo"

    async def is_available(self, db: AsyncSession) -> bool:
        return bool((await get_apollo_api_key(db)).strip())

    async def enrich(self, db, *, domain, company_name=""):
        api_key = (await get_apollo_api_key(db)).strip()
        if not api_key:
            return [], {}, "no_apollo_key"
        if not domain:
            return [], {}, "no_domain"

        clean_domain = (domain or "").replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"{APOLLO_BASE}/mixed_people/search",
                    headers={
                        "X-Api-Key": api_key,
                        "Content-Type": "application/json",
                        "accept": "application/json",
                    },
                    json={
                        "q_organization_domains": [clean_domain],
                        "person_titles": list(APOLLO_TARGET_TITLES),
                        "page": 1,
                        "per_page": 10,
                    },
                )
        except httpx.HTTPError as e:
            log.warning(f"Apollo network error for {clean_domain}: {e}")
            return [], {}, f"network_error: {e}"

        if response.status_code == 401:
            return [], {}, "apollo_unauthorized — bad key?"
        if response.status_code == 429:
            return [], {}, "apollo_rate_limited"
        if response.status_code >= 400:
            return [], {}, f"apollo_http_{response.status_code}"

        try:
            data = response.json() or {}
        except Exception:
            return [], {}, "apollo_invalid_json"

        people = data.get("people") or []
        contacts: list[WaterfallContact] = []
        company_patch: dict = {}

        for p in people:
            email = (p.get("email") or "").strip().lower()
            if not email or "@" not in email:
                continue

            # Apollo phone numbers come as a list of {raw_number, type} dicts.
            # 'type' = 'mobile' | 'work' | 'other'. Mobile is the gold one.
            phones = p.get("phone_numbers") or []
            mobile = next((ph.get("raw_number") for ph in phones if (ph.get("type") or "").lower() == "mobile"), None)
            any_phone = next((ph.get("raw_number") for ph in phones if ph.get("raw_number")), None)

            contacts.append(WaterfallContact(
                email=email,
                full_name=(p.get("name") or f"{p.get('first_name','')} {p.get('last_name','')}").strip() or None,
                first_name=(p.get("first_name") or "").strip() or None,
                last_name=(p.get("last_name") or "").strip() or None,
                job_title=(p.get("title") or "").strip() or None,
                phone=any_phone if any_phone != mobile else None,
                mobile_phone=mobile,
                linkedin_url=(p.get("linkedin_url") or "").strip() or None,
                email_status=(p.get("email_status") or "unknown").lower(),
                source="apollo",
                confidence=90 if (p.get("email_status") in ("verified", "valid")) else 60,
            ))

        # Apollo also returns organization-level data in the search response —
        # capture employee count + industry if present so we can fill gaps
        # the platform's other enrichment didn't catch.
        org = (people[0].get("organization") if people else None) or {}
        if org:
            if org.get("estimated_num_employees"):
                company_patch["employee_count"] = int(org["estimated_num_employees"])
            if org.get("industry"):
                company_patch["industry"] = org["industry"]
            if org.get("linkedin_url"):
                company_patch["linkedin_url"] = org["linkedin_url"]

        # Meter the call. Apollo BYO-key — tenant pays Apollo directly,
        # we charge a small orchestration fee in credits.
        try:
            from app.services.credit_meter import meter_standalone, make_idem_key
            await meter_standalone(
                action_type="enrich_apollo",
                idempotency_key=make_idem_key("enrich_apollo", clean_domain),
                action_ref=f"apollo:{clean_domain}",
                metadata={"contacts_found": len(contacts)},
            )
        except Exception:
            pass

        return contacts, company_patch, None


# ============================================================
# Netrows provider — platform-paid, decision-maker endpoint
# ============================================================

class NetrowsProvider:
    name = "netrows_dm"

    async def is_available(self, db):
        return bool((await get_netrows_api_key(db)).strip())

    async def enrich(self, db, *, domain, company_name=""):
        api_key = await get_netrows_api_key(db)
        if not api_key:
            return [], {}, "no_netrows_key"
        if not domain:
            return [], {}, "no_domain"

        try:
            nr = await netrows_find_decision_makers(domain, api_key)
        except Exception as e:
            return [], {}, f"netrows_error: {e}"

        if nr.error:
            return [], {}, nr.error

        contacts = [
            WaterfallContact(
                email=(dm.email or "").strip().lower(),
                full_name=dm.full_name,
                job_title=dm.job_title,
                linkedin_url=dm.linkedin_url,
                email_status=(dm.email_status or "unknown").lower(),
                source="netrows_dm",
                confidence=85 if (dm.email_status or "").lower() == "valid" else 60,
            )
            for dm in nr.decision_makers if dm.email
        ]

        # Generic emails (info@, hello@, office@) are low-confidence but
        # still worth capturing — many SMBs only have these.
        for generic in (nr.generic_emails or []):
            email = (generic or "").strip().lower()
            if email and "@" in email:
                contacts.append(WaterfallContact(
                    email=email, source="netrows_dm",
                    job_title=None, confidence=30,
                    email_status="unknown",
                ))

        # Metering already happens inside the existing enrich-company route;
        # we don't double-count here. Future: move metering into provider.
        return contacts, {}, None


# ============================================================
# Hunter provider — platform-paid, generic email finder
# ============================================================

class HunterProvider:
    name = "hunter"

    async def is_available(self, db):
        return bool((settings.hunter_api_key or "").strip())

    async def enrich(self, db, *, domain, company_name=""):
        if not settings.hunter_api_key:
            return [], {}, "no_hunter_key"
        if not domain:
            return [], {}, "no_domain"

        try:
            res = await hunter_search(domain, settings.hunter_api_key)
        except Exception as e:
            return [], {}, f"hunter_error: {e}"

        contacts = []
        for hc in res.contacts or []:
            email = (hc.email or "").strip().lower()
            if not email:
                continue
            full = f"{hc.first_name or ''} {hc.last_name or ''}".strip() or None
            contacts.append(WaterfallContact(
                email=email,
                full_name=full,
                first_name=(hc.first_name or "").strip() or None,
                last_name=(hc.last_name or "").strip() or None,
                job_title=(hc.position or "").strip() or None,
                source="hunter",
                confidence=hc.confidence or 30,
            ))

        company_patch = {}
        if res.organization:
            company_patch["organization_name_hunter"] = res.organization

        return contacts, company_patch, None


# ============================================================
# Waterfall orchestrator
# ============================================================

# Provider classes in priority order. First-match wins for contact dedup;
# later providers fill in fields the earlier ones left blank.
DEFAULT_PROVIDER_ORDER: list[type] = [ApolloProvider, NetrowsProvider, HunterProvider]


class EnrichmentWaterfall:
    """Run providers in priority order, dedupe by email, merge metadata."""

    def __init__(self, providers: Optional[list[EnrichmentProvider]] = None):
        if providers is None:
            providers = [cls() for cls in DEFAULT_PROVIDER_ORDER]
        self.providers = providers

    async def enrich(
        self,
        db: AsyncSession,
        *,
        domain: str,
        company_name: str = "",
    ) -> WaterfallResult:
        result = WaterfallResult()
        # Email-keyed accumulator; first writer wins, later providers
        # only fill in null fields (so Apollo's name + phone don't get
        # overwritten by Hunter's empty-name shell).
        by_email: dict[str, WaterfallContact] = {}

        for provider in self.providers:
            try:
                if not await provider.is_available(db):
                    continue
            except Exception as e:
                result.errors[provider.name] = f"availability_check_failed: {e}"
                continue

            result.providers_called.append(provider.name)
            try:
                contacts, company_patch, err = await provider.enrich(
                    db, domain=domain, company_name=company_name,
                )
            except Exception as e:
                # Defense in depth — providers should never raise, but
                # we belt-and-suspender catch here so one bad provider
                # can't poison the cascade.
                result.errors[provider.name] = f"unhandled: {e}"
                continue

            if err:
                result.errors[provider.name] = err

            for c in contacts:
                if not c.email:
                    continue
                key = c.email.lower()
                existing = by_email.get(key)
                if existing is None:
                    by_email[key] = c
                else:
                    # Fill in fields existing left null
                    for field_name in (
                        "full_name", "first_name", "last_name", "job_title",
                        "phone", "mobile_phone", "linkedin_url",
                    ):
                        if not getattr(existing, field_name) and getattr(c, field_name):
                            setattr(existing, field_name, getattr(c, field_name))
                    # Upgrade email_status if a later provider verified it
                    if existing.email_status in ("unknown", "") and c.email_status not in ("unknown", ""):
                        existing.email_status = c.email_status

            # Merge company-level data: first non-null wins
            for k, v in (company_patch or {}).items():
                if v and not result.company_data.get(k):
                    result.company_data[k] = v

        result.contacts = list(by_email.values())
        # Sort: highest confidence first, then mobile-phone-having first
        result.contacts.sort(
            key=lambda c: (c.confidence, 1 if c.mobile_phone else 0),
            reverse=True,
        )
        return result
