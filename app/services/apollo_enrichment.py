"""
Apollo.io Enrichment Service
Finds decision makers (owners, managers) at a business using their domain.
"""
from __future__ import annotations
from typing import Optional, List
import httpx
from dataclasses import dataclass, field


@dataclass
class ContactInfo:
    name: str
    title: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    linkedin_url: Optional[str] = None


@dataclass
class ApolloEnrichment:
    company_name: Optional[str] = None
    industry: Optional[str] = None
    employee_count: Optional[int] = None
    city: Optional[str] = None
    state: Optional[str] = None
    contacts: List[ContactInfo] = field(default_factory=list)


async def enrich_from_domain(domain: str, api_key: str) -> ApolloEnrichment:
    """
    Look up a company by domain in Apollo and find decision makers.
    Prioritizes owners, founders, presidents, GMs, and marketing managers.
    """
    result = ApolloEnrichment()

    # Clean domain
    domain = domain.replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")

    async with httpx.AsyncClient(timeout=15) as client:
        # Step 1: Find the organization
        org_response = await client.post(
            "https://api.apollo.io/api/v1/organizations/enrich",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            json={"domain": domain},
        )

        if org_response.status_code == 200:
            org_data = org_response.json().get("organization", {})
            result.company_name = org_data.get("name")
            result.industry = org_data.get("industry")
            result.employee_count = org_data.get("estimated_num_employees")
            result.city = org_data.get("city")
            result.state = org_data.get("state")

        # Step 2: Search for people at this domain — prioritize decision makers
        people_response = await client.post(
            "https://api.apollo.io/api/v1/mixed_people/search",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            json={
                "q_organization_domains": domain,
                "person_titles": [
                    "owner", "founder", "president", "ceo",
                    "general manager", "manager", "director",
                    "marketing", "vp",
                ],
                "page": 1,
                "per_page": 5,
            },
        )

        if people_response.status_code == 200:
            people_data = people_response.json().get("people", [])
            for person in people_data:
                contact = ContactInfo(
                    name=person.get("name", ""),
                    title=person.get("title"),
                    email=person.get("email"),
                    phone=_get_best_phone(person),
                    linkedin_url=person.get("linkedin_url"),
                )
                if contact.name:
                    result.contacts.append(contact)

    return result


def _get_best_phone(person: dict) -> Optional[str]:
    """Extract the best phone number from Apollo person data."""
    # Try direct dial first, then mobile, then company
    phone_numbers = person.get("phone_numbers", [])
    for phone in phone_numbers:
        if phone.get("type") == "mobile":
            return phone.get("sanitized_number")
    for phone in phone_numbers:
        if phone.get("type") == "work_direct":
            return phone.get("sanitized_number")
    if phone_numbers:
        return phone_numbers[0].get("sanitized_number")
    return None
