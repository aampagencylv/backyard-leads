"""
Hunter.io Email Finder Service
Finds email addresses associated with a domain.
Better than Apollo for small local businesses because it scrapes
the web for email patterns rather than relying on a contact database.
"""
from __future__ import annotations
from typing import Optional, List
import httpx
from dataclasses import dataclass, field


@dataclass
class HunterContact:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    position: Optional[str] = None
    confidence: int = 0
    type: str = ""  # personal or generic


@dataclass
class HunterResult:
    domain: str
    organization: Optional[str] = None
    emails_found: int = 0
    pattern: Optional[str] = None  # e.g. "{first}@domain.com"
    contacts: List[HunterContact] = field(default_factory=list)
    best_guess_email: Optional[str] = None


async def search_domain(domain: str, api_key: str, limit: int = 10) -> HunterResult:
    """
    Search Hunter.io for all emails associated with a domain.
    Returns contacts found + the email pattern for the domain.
    """
    result = HunterResult(domain=domain)

    # Clean domain
    domain = domain.replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")
    result.domain = domain

    async with httpx.AsyncClient(timeout=15) as client:
        # Domain search — find all emails at this domain.
        # Hunter Free plan caps results at 10; Starter at 50; higher plans up to 100.
        # We retry with a lower limit if the API rejects the request.
        response = await client.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": api_key, "limit": limit},
        )
        if response.status_code == 400 and "pagination_error" in response.text and limit > 10:
            response = await client.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "api_key": api_key, "limit": 10},
            )

        if response.status_code == 200:
            data = response.json().get("data", {})
            result.organization = data.get("organization")
            result.pattern = data.get("pattern")
            result.emails_found = data.get("total", 0) if isinstance(data.get("total"), int) else 0

            for email_data in data.get("emails", []):
                contact = HunterContact(
                    email=email_data.get("value", ""),
                    first_name=email_data.get("first_name"),
                    last_name=email_data.get("last_name"),
                    position=email_data.get("position"),
                    confidence=email_data.get("confidence", 0),
                    type=email_data.get("type", ""),
                )
                if contact.email:
                    result.contacts.append(contact)

    # Sort by confidence, personal emails first
    result.contacts.sort(
        key=lambda c: (0 if c.type == "personal" else 1, -c.confidence)
    )

    return result


async def find_email(
    domain: str,
    first_name: str,
    last_name: str,
    api_key: str,
) -> Optional[str]:
    """
    Use Hunter's email finder to guess an email for a specific person at a domain.
    """
    domain = domain.replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            "https://api.hunter.io/v2/email-finder",
            params={
                "domain": domain,
                "first_name": first_name,
                "last_name": last_name,
                "api_key": api_key,
            },
        )

        if response.status_code == 200:
            data = response.json().get("data", {})
            email = data.get("email")
            confidence = data.get("confidence", 0)
            if email and confidence >= 30:
                return email

    return None


async def verify_email(email: str, api_key: str) -> dict:
    """
    Verify an email address via Hunter's /v2/email-verifier.
    Returns: {"result": "deliverable"|"risky"|"undeliverable"|"unknown",
              "score": int, "smtp_check": bool, ...}
    """
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://api.hunter.io/v2/email-verifier",
            params={"email": email, "api_key": api_key},
        )
        if response.status_code == 200:
            return response.json().get("data", {})
    return {}
