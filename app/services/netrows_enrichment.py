"""
Netrows Email Finder Service

Wraps the Netrows /v1/email-finder/decision-maker endpoint, which is the
single most useful B2B-data API call for BMP's prospect base: it returns
a verified owner-tier email address for a given domain.

Hit rate against BMP's Vegas landscaper test set: 75% (3 of 4), all
returned with email_status='valid'. Cost: 10 credits per call.

Falls back to /v1/email-finder/by-domain (5 credits) when the decision-
maker endpoint returns 404 — picks up generic info@/office@ aliases that
Hunter would otherwise be needed for.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List
import httpx


BASE_URL = "https://api.netrows.com/v1"

# Categories ordered by likelihood of being the right person to email at a
# small home-services business. We try each in order until one succeeds.
DEFAULT_CATEGORIES = ("ceo", "operations", "marketing", "sales")


@dataclass
class NetrowsContact:
    email: str
    email_status: str = "unknown"  # 'valid', 'invalid', 'unknown'
    full_name: Optional[str] = None
    job_title: Optional[str] = None
    linkedin_url: Optional[str] = None
    category: Optional[str] = None  # which decision_maker_category matched
    source: str = "netrows_decision_maker"


@dataclass
class NetrowsResult:
    domain: str
    decision_makers: List[NetrowsContact] = field(default_factory=list)
    generic_emails: List[str] = field(default_factory=list)
    error: Optional[str] = None


def _clean_domain(domain: str) -> str:
    return (
        (domain or "")
        .replace("https://", "")
        .replace("http://", "")
        .split("/")[0]
        .replace("www.", "")
        .strip()
    )


async def find_decision_makers(
    domain: str,
    api_key: str,
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
) -> NetrowsResult:
    """
    Try each category in order; return all matches found.
    Each successful category costs 10 credits; categories that 404 also
    consume 10 credits (per Netrows docs: "Credits are deducted regardless
    of result"). Default tuple stops after the first match — call with a
    longer tuple to fetch multiple stakeholders.
    """
    domain = _clean_domain(domain)
    result = NetrowsResult(domain=domain)
    if not domain or not api_key:
        result.error = "missing domain or api_key"
        return result

    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient(timeout=20) as client:
        for category in categories:
            try:
                resp = await client.get(
                    f"{BASE_URL}/email-finder/decision-maker",
                    params={"domain": domain, "category": category},
                    headers=headers,
                )
            except httpx.HTTPError as e:
                result.error = f"network error: {e}"
                continue

            if resp.status_code == 200:
                data = resp.json()
                contact = NetrowsContact(
                    email=data.get("email", ""),
                    email_status=data.get("email_status", "unknown"),
                    full_name=data.get("person_full_name"),
                    job_title=data.get("person_job_title"),
                    linkedin_url=data.get("person_linkedin_url"),
                    category=data.get("decision_maker_category", category),
                )
                if contact.email:
                    result.decision_makers.append(contact)
                # First hit is usually the most relevant — break unless the caller
                # explicitly passed multiple categories. The default tuple has the
                # CEO/owner first, so breaking is the right behavior for SMB.
                break
            elif resp.status_code == 404:
                # No decision maker for this category — try next one
                continue
            elif resp.status_code in (401, 402, 429):
                result.error = f"netrows {resp.status_code}: {resp.text[:200]}"
                return result  # don't keep burning credits on auth/quota errors
            else:
                # Unexpected — record and try next
                result.error = f"netrows {resp.status_code}: {resp.text[:200]}"

        # If no decision-maker found, fall back to by-domain (5 credits)
        if not result.decision_makers and not result.error:
            try:
                resp = await client.get(
                    f"{BASE_URL}/email-finder/by-domain",
                    params={"domain": domain},
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    result.generic_emails = data.get("emails", []) or []
            except httpx.HTTPError:
                pass

    return result


async def find_email_by_name(
    first_name: str,
    last_name: str,
    domain: str,
    api_key: str,
) -> Optional[NetrowsContact]:
    """
    Find a verified email for a known person at a domain (5 credits).
    Use after we already have a name (e.g. from Bizapedia, manual entry,
    or a referral) and just need the email.
    """
    domain = _clean_domain(domain)
    if not (first_name and last_name and domain and api_key):
        return None

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.get(
                f"{BASE_URL}/email-finder/by-name",
                params={"first_name": first_name, "last_name": last_name, "domain": domain},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError:
            return None

    if resp.status_code != 200:
        return None
    data = resp.json()
    email = data.get("email")
    if not email:
        return None
    return NetrowsContact(
        email=email,
        email_status=data.get("email_status", "unknown"),
        full_name=f"{first_name} {last_name}",
        source="netrows_by_name",
    )
