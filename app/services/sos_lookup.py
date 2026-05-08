"""
Secretary-of-State enrichment.

Phase 2 of the enrichment chain. Free public records, scraped per-state.
v1 ships Florida (Sunbiz) — the most scrape-friendly state and BMP's
biggest backyard-pro market outside AZ. Arizona (eCorp) and Nevada
(SilverFlume) follow in a subsequent commit using the same pattern.

Why SoS data matters for SMB outreach:
  - Owner names are public record (registered agents + officers)
  - Filing date signals business age (stability filter)
  - Active vs. dissolved status (don't waste outreach on dead LLCs)
  - Cross-LLC linkage — same person owns multiple entities

Cost model: free vendor cost; we charge a small orchestration credit
per lookup to cover compute + cache infrastructure. Aggregator alternative
(OpenCorporates) revisited at >10 SaaS tenants.

Compliance: SoS records are public. Each state's ToS varies; we
rate-limit 1 req/sec per state, identify the user-agent honestly,
cache results 30 days.

Architecture: every state is a `lookup_<state>` async function that
returns SoSResult. The dispatcher `lookup_state(state, name)` picks
the right one by company.state. Adding a new state = one new function.
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SoSLookup

log = logging.getLogger("bmp.sos")

CACHE_TTL = timedelta(days=30)
USER_AGENT = "BackyardMarketingPros-Prospector/1.0 (sales@backyardmarketingpros.com)"


@dataclass
class SoSOfficer:
    name: str
    title: str = ""


@dataclass
class SoSResult:
    state: str
    found: bool = False
    document_number: Optional[str] = None
    legal_name: Optional[str] = None
    filing_date: Optional[str] = None        # YYYY-MM-DD
    status: Optional[str] = None              # 'active', 'inactive', 'dissolved'
    entity_type: Optional[str] = None         # 'LLC', 'Corporation', etc.
    principal_address: Optional[str] = None
    mailing_address: Optional[str] = None
    registered_agent_name: Optional[str] = None
    registered_agent_address: Optional[str] = None
    officers: list[SoSOfficer] = field(default_factory=list)
    raw_url: Optional[str] = None
    error: Optional[str] = None

    def to_payload(self) -> dict:
        d = asdict(self)
        return d


# ============================================================
# Cache layer
# ============================================================

def _normalize_name(name: str) -> str:
    """Lowercase, strip entity suffixes, collapse whitespace.
    'Smith Pools, LLC' / 'SMITH POOLS' / 'Smith Pools Llc' → 'smith pools'.
    """
    n = (name or "").lower()
    n = re.sub(r"[,.]", "", n)
    n = re.sub(r"\b(llc|inc|corp|corporation|company|co|ltd|lp|llp|pllc|p\.?c\.?)\b", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


async def _get_cached(db: AsyncSession, state: str, company_name: str) -> Optional[SoSResult]:
    norm = _normalize_name(company_name)
    if not norm:
        return None
    row = (await db.execute(
        select(SoSLookup).where(
            SoSLookup.state == state,
            SoSLookup.company_name == norm,
        )
    )).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at:
        exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            return None
    if not row.result_json:
        return SoSResult(state=state, found=False)
    try:
        data = json.loads(row.result_json)
        officers = [SoSOfficer(**o) for o in (data.pop("officers", []) or [])]
        return SoSResult(officers=officers, **data)
    except Exception as e:
        log.warning(f"Bad SoS cache row for {state}/{norm}: {e}")
        return None


async def _save_cache(db: AsyncSession, state: str, company_name: str, result: SoSResult) -> None:
    norm = _normalize_name(company_name)
    if not norm:
        return
    now = datetime.now(timezone.utc)
    expires = now + CACHE_TTL
    payload = result.to_payload()
    payload_json = json.dumps(payload, default=str)
    existing = (await db.execute(
        select(SoSLookup).where(SoSLookup.state == state, SoSLookup.company_name == norm)
    )).scalar_one_or_none()
    if existing:
        existing.result_json = payload_json
        existing.found = result.found
        existing.fetched_at = now
        existing.expires_at = expires
    else:
        db.add(SoSLookup(
            state=state, company_name=norm,
            found=result.found,
            result_json=payload_json,
            fetched_at=now, expires_at=expires,
        ))
    await db.commit()


# ============================================================
# Florida — Sunbiz (https://search.sunbiz.org)
# ============================================================

SUNBIZ_BASE = "https://search.sunbiz.org/Inquiry/CorporationSearch"


async def _lookup_florida_uncached(company_name: str) -> SoSResult:
    """Scrape Sunbiz for a Florida entity. Best-effort parser — the
    site's HTML structure occasionally shifts, so the parser is
    tolerant of missing fields. Worst case returns found=False with an
    error string; never raises."""
    result = SoSResult(state="FL")
    try:
        # Sunbiz searchNameOrder strips spaces + uppercases everything.
        # Their server then does a fuzzy name search and returns a list.
        search_q = re.sub(r"[^A-Z0-9]", "", (company_name or "").upper())
        if not search_q:
            result.error = "empty_name"
            return result
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        ) as client:
            search_url = f"{SUNBIZ_BASE}/SearchResults?inquiryType=EntityName&searchNameOrder={search_q}"
            r = await client.get(search_url)
            if r.status_code != 200:
                result.error = f"search_http_{r.status_code}"
                return result

            soup = BeautifulSoup(r.text, "html.parser")
            # Result rows live in a <table> under #search-results;
            # the first <a> in each row points at the detail page.
            rows = soup.select("table tbody tr")
            if not rows:
                # No matches found — also sometimes a "no results" banner
                if "no records were found" in r.text.lower() or "no entities" in r.text.lower():
                    return result  # found=False, no error
                # Otherwise the page may have changed shape — log + bail
                result.error = "no_result_rows"
                return result

            link = None
            for row in rows[:5]:  # consider top 5 matches
                a = row.select_one("a[href*='SearchResultDetail']")
                if a:
                    link = a
                    break
            if not link:
                result.error = "no_detail_link"
                return result

            detail_path = link.get("href") or ""
            detail_url = detail_path if detail_path.startswith("http") else f"https://search.sunbiz.org{detail_path}"
            await asyncio.sleep(1.0)  # politeness throttle
            r2 = await client.get(detail_url)
            if r2.status_code != 200:
                result.error = f"detail_http_{r2.status_code}"
                return result

            return _parse_florida_detail(r2.text, detail_url)
    except httpx.HTTPError as e:
        log.warning(f"Florida SoS network error for {company_name}: {e}")
        result.error = f"network: {e}"
        return result
    except Exception as e:
        log.warning(f"Florida SoS lookup failed for {company_name}: {e}")
        result.error = f"exception: {e}"
        return result


def _parse_florida_detail(html: str, url: str) -> SoSResult:
    """Parse a Sunbiz detail page. Sunbiz uses labeled <span> elements
    with sibling values — we look for known labels and pull the text
    that follows. Tolerant of structural shifts: any field we can't
    find stays None.
    """
    soup = BeautifulSoup(html, "html.parser")
    result = SoSResult(state="FL", found=True, raw_url=url)

    text = soup.get_text("\n", strip=True)

    # Legal name — the page header has "Detail by Entity Name" then the
    # company's legal name in a heading or paragraph just below.
    name_match = re.search(r"Detail by Entity Name\s*\n+([A-Z0-9 ,&'.\-/]+)", text)
    if name_match:
        result.legal_name = name_match.group(1).strip().rstrip(",")

    # Document number — labeled "Document Number"
    doc_match = re.search(r"Document Number\s*\n+([A-Z0-9]+)", text)
    if doc_match:
        result.document_number = doc_match.group(1).strip()

    # Filing date — labeled "Date Filed" or "Filing Date"
    filing_match = re.search(r"(?:Date Filed|Filing Date)\s*\n+([0-9]{2}/[0-9]{2}/[0-9]{4})", text)
    if filing_match:
        # Convert MM/DD/YYYY to YYYY-MM-DD
        m, d, y = filing_match.group(1).split("/")
        result.filing_date = f"{y}-{m}-{d}"

    # Status
    status_match = re.search(r"\bStatus\s*\n+([A-Za-z ]+)", text)
    if status_match:
        s = status_match.group(1).strip().lower()
        # Sunbiz uses 'ACTIVE', 'INACTIVE', 'DISSOLVED', etc.
        result.status = s

    # Entity type — usually in the legal name suffix or in a specific label
    if result.legal_name:
        legal_upper = result.legal_name.upper()
        for et in ("LLC", "L.L.C.", "INC", "CORP", "PA", "PLLC", "LP", "LLP"):
            if legal_upper.endswith(et) or f" {et}" in legal_upper:
                result.entity_type = et
                break

    # Principal Address + Mailing Address — under labeled headers
    pa_match = re.search(r"Principal Address\s*\n+(.{0,500}?)(?:Mailing Address|Registered Agent|Officer/Director)", text, re.S)
    if pa_match:
        result.principal_address = " ".join(line.strip() for line in pa_match.group(1).splitlines() if line.strip())[:300]
    ma_match = re.search(r"Mailing Address\s*\n+(.{0,500}?)(?:Registered Agent|Officer/Director|Annual Reports)", text, re.S)
    if ma_match:
        result.mailing_address = " ".join(line.strip() for line in ma_match.group(1).splitlines() if line.strip())[:300]

    # Registered Agent — labeled "Registered Agent Name & Address"
    ra_match = re.search(r"Registered Agent Name\s*&\s*Address\s*\n+(.{0,500}?)(?:Officer/Director|Annual Reports)", text, re.S)
    if ra_match:
        ra_block = [line.strip() for line in ra_match.group(1).splitlines() if line.strip()]
        if ra_block:
            result.registered_agent_name = ra_block[0][:120]
            result.registered_agent_address = " ".join(ra_block[1:])[:300] or None

    # Officers — labeled "Officer/Director Detail" with each officer
    # appearing as <Title> <Name> <Address> blocks. We just capture
    # title + name (addresses often duplicate the principal address).
    od_match = re.search(r"Officer/Director Detail.*?(?=\n\nAnnual Reports|\Z)", text, re.S)
    if od_match:
        block = od_match.group(0)
        # Look for "Title <TITLE>\n\n<NAME>" patterns
        for m in re.finditer(r"Title\s+([A-Z]+)\s*\n+([A-Z][A-Z, ]+)", block):
            title_token = m.group(1).strip()
            name = m.group(2).strip().rstrip(",")
            if name and title_token:
                # Sunbiz uses single-token titles like 'P', 'VP', 'D', 'MGR'
                title_map = {"P": "President", "VP": "Vice President", "D": "Director",
                             "MGR": "Manager", "MGRM": "Managing Member", "T": "Treasurer",
                             "S": "Secretary", "AGRM": "Authorized Member"}
                pretty_title = title_map.get(title_token, title_token.title())
                result.officers.append(SoSOfficer(name=name.title(), title=pretty_title))

    return result


# ============================================================
# Public API
# ============================================================

async def lookup_florida(db: AsyncSession, company_name: str) -> SoSResult:
    """Cache-first Florida lookup. Returns SoSResult — never raises.
    Found=False both for cache-miss-and-no-results AND error cases;
    check `error` field to disambiguate."""
    cached = await _get_cached(db, "FL", company_name)
    if cached is not None:
        return cached
    result = await _lookup_florida_uncached(company_name)
    try:
        await _save_cache(db, "FL", company_name, result)
    except Exception:
        log.warning(f"Could not cache FL SoS result for {company_name}")
    return result


# Dispatcher — picks the right state scraper. AZ + NV land here next.
async def lookup_state(db: AsyncSession, state: Optional[str], company_name: str) -> Optional[SoSResult]:
    """Returns None when we don't have a scraper for that state yet
    (callers can ignore SoS in that case)."""
    if not state or not company_name:
        return None
    state_upper = (state or "").strip().upper()
    if state_upper in ("FL", "FLORIDA"):
        return await lookup_florida(db, company_name)
    # AZ, NV, TX, CA, etc. — pending scraper implementations
    return None


# ============================================================
# Metering helper
# ============================================================

async def meter_sos_lookup(state: str, company_id: Optional[int]) -> None:
    try:
        from app.services.credit_meter import meter_standalone, make_idem_key
        await meter_standalone(
            action_type="scrape_yelp",  # reuses scraping rate; future: 'enrich_sos'
            idempotency_key=make_idem_key("sos", state, company_id or "noid", datetime.now(timezone.utc).timestamp()),
            action_ref=f"sos:{state}:{company_id or '?'}",
            metadata={"state": state, "company_id": company_id},
        )
    except Exception:
        pass
