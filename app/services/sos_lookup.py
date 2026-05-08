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
    address: Optional[str] = None  # Personal/principal address — feeds skip-trace → cell phone


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
    last_annual_report_date: Optional[str] = None  # YYYY-MM-DD; operational-health filter
    dba_names: list[str] = field(default_factory=list)  # Trade names / fictitious names — join key to brand
    years_in_business: Optional[int] = None  # Derived from filing_date for lead scoring
    raw_url: Optional[str] = None
    error: Optional[str] = None

    def to_payload(self) -> dict:
        d = asdict(self)
        return d


def _derive_years_in_business(filing_date: Optional[str]) -> Optional[int]:
    """filing_date is YYYY-MM-DD. Returns whole years from filing → today."""
    if not filing_date:
        return None
    try:
        f = datetime.strptime(filing_date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - f
        years = delta.days // 365
        return years if years >= 0 else None
    except Exception:
        return None


def _log_field_coverage(state: str, result: "SoSResult") -> None:
    """Per-state per-field coverage telemetry. Logs which Tier-1 fields
    we got — aggregated across calls, this tells us which state parsers
    need tightening and which states reliably expose which fields.
    Logged at INFO so we can grep prod logs after the first ~50 calls."""
    if not result.found:
        return
    fields = {
        "doc": bool(result.document_number),
        "name": bool(result.legal_name),
        "filed": bool(result.filing_date),
        "yib": result.years_in_business is not None,
        "status": bool(result.status),
        "type": bool(result.entity_type),
        "principal_addr": bool(result.principal_address),
        "agent_name": bool(result.registered_agent_name),
        "agent_addr": bool(result.registered_agent_address),
        "officers": len(result.officers),
        "officer_addrs": sum(1 for o in result.officers if o.address),
        "last_annual": bool(result.last_annual_report_date),
        "dbas": len(result.dba_names),
    }
    log.info(f"sos_coverage state={state} {fields}")


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
    # appearing as <Title> <Name>\n<Address> blocks. We capture
    # title + name + address; the personal address is the skip-trace
    # seed for cell-phone lookups (the highest-value SoS field for
    # owner-operator SMBs like backyard pros).
    od_match = re.search(r"Officer/Director Detail.*?(?=\n\nAnnual Reports|\Z)", text, re.S)
    if od_match:
        block = od_match.group(0)
        # Capture: Title token, name line, then up-to-3 lines of address
        # before the next "Title" or section break.
        title_map = {"P": "President", "VP": "Vice President", "D": "Director",
                     "MGR": "Manager", "MGRM": "Managing Member", "T": "Treasurer",
                     "S": "Secretary", "AGRM": "Authorized Member", "CEO": "CEO",
                     "CFO": "CFO", "COO": "COO", "AMBR": "Authorized Member"}
        # Greedy enough to grab address lines, lazy enough to stop at
        # the next "Title" marker or end of section.
        for m in re.finditer(
            r"Title\s+([A-Z]+)\s*\n+([A-Z][A-Z, ]+)\s*\n+((?:[^\n]+\n){0,4}?)(?=Title\s+[A-Z]+|\Z)",
            block,
        ):
            title_token = m.group(1).strip()
            name = m.group(2).strip().rstrip(",")
            addr_block = m.group(3)
            if not (name and title_token):
                continue
            pretty_title = title_map.get(title_token, title_token.title())
            # Address lines until we hit something that looks like a
            # state/zip (last line) — keep first 3 non-empty lines.
            lines = [ln.strip() for ln in addr_block.splitlines() if ln.strip()]
            address = ", ".join(lines[:3]) if lines else None
            if address:
                # Drop accidental "Title X" or stray bracket text
                if re.match(r"^Title\s+", address):
                    address = None
            result.officers.append(SoSOfficer(name=name.title(), title=pretty_title, address=address))

    # Last annual report — Sunbiz lists "Annual Reports" with a table
    # of years + filed dates. Grab the most recent.
    ar_match = re.search(r"Annual Reports.*?(?=Document Images|\Z)", text, re.S)
    if ar_match:
        # Lines look like: "2024  04/15/2024  ..."
        dates = re.findall(r"\b(\d{2})/(\d{2})/(\d{4})\b", ar_match.group(0))
        if dates:
            # Pick the latest date
            parsed = sorted({(int(y), int(m), int(d)) for m, d, y in dates}, reverse=True)
            y, m, d = parsed[0]
            result.last_annual_report_date = f"{y:04d}-{m:02d}-{d:02d}"

    # Years in business — derived from filing_date
    result.years_in_business = _derive_years_in_business(result.filing_date)

    return result


# ============================================================
# Arizona — eCorp (https://ecorp.azcc.gov)
# ============================================================
#
# Arizona Corporation Commission's eCorp is a modern SPA backed by a
# JSON API. The search endpoint accepts a JSON POST and returns a list
# of matching entities; we then GET the detail page (also JSON) by
# entity number. If AZCC ships a UI rewrite, the API tends to stay
# stable longer than HTML, but we still parse defensively.
#
# Untested against live data on first ship — returns found=False on
# any structural anomaly so we can iterate based on real responses
# rather than poisoning records.

AZCC_SEARCH_URL = "https://ecorp.azcc.gov/Services/Entity/SearchEntity"
AZCC_DETAIL_URL = "https://ecorp.azcc.gov/Services/Entity/GetEntityDetails"


async def _lookup_arizona_uncached(company_name: str) -> SoSResult:
    result = SoSResult(state="AZ")
    try:
        if not (company_name or "").strip():
            result.error = "empty_name"
            return result
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://ecorp.azcc.gov",
                "Referer": "https://ecorp.azcc.gov/EntitySearch",
            },
        ) as client:
            # AZCC's search payload — best-effort shape; field names taken
            # from the eCorp UI's network calls.
            payload = {
                "entityName": company_name.strip(),
                "entityNumber": "",
                "agentName": "",
                "principalName": "",
                "exactMatch": False,
                "activeOnly": False,
            }
            r = await client.post(AZCC_SEARCH_URL, json=payload)
            if r.status_code != 200:
                result.error = f"search_http_{r.status_code}"
                return result

            try:
                data = r.json()
            except Exception:
                result.error = "search_not_json"
                log.info(f"AZ SoS unexpected non-JSON for {company_name}: {r.text[:300]}")
                return result

            # Response can be either {"results": [...]} or a bare list
            rows = data.get("results") if isinstance(data, dict) else data
            if not isinstance(rows, list) or not rows:
                return result  # found=False

            # Take top match (AZCC sorts by relevance). Conservative: if
            # the top match's name has zero token overlap with the query,
            # bail rather than guess.
            top = rows[0]
            top_name = (top.get("entityName") or top.get("name") or "").strip()
            if top_name and not _name_token_overlap(top_name, company_name):
                result.error = "no_close_match"
                log.info(f"AZ SoS top match {top_name!r} far from query {company_name!r}")
                return result

            entity_number = (top.get("entityNumber") or top.get("entityID")
                             or top.get("entityId") or "").strip()
            if not entity_number:
                result.error = "no_entity_number"
                return result

            await asyncio.sleep(1.0)  # politeness throttle

            r2 = await client.get(AZCC_DETAIL_URL, params={"entityNumber": entity_number})
            if r2.status_code != 200:
                result.error = f"detail_http_{r2.status_code}"
                return result
            try:
                detail = r2.json()
            except Exception:
                result.error = "detail_not_json"
                log.info(f"AZ SoS unexpected non-JSON detail for {entity_number}: {r2.text[:300]}")
                return result

            return _parse_arizona_detail(detail, entity_number)
    except httpx.HTTPError as e:
        log.warning(f"Arizona SoS network error for {company_name}: {e}")
        result.error = f"network: {e}"
        return result
    except Exception as e:
        log.warning(f"Arizona SoS lookup failed for {company_name}: {e}")
        result.error = f"exception: {e}"
        return result


def _parse_arizona_detail(data: dict, entity_number: str) -> SoSResult:
    """Defensively map AZCC JSON detail to SoSResult. Field names are
    inferred from public eCorp pages; missing fields leave defaults."""
    result = SoSResult(
        state="AZ", found=True,
        document_number=entity_number,
        raw_url=f"https://ecorp.azcc.gov/EntitySearch/Entity?entityNumber={entity_number}",
    )
    if not isinstance(data, dict):
        return SoSResult(state="AZ", error="detail_not_object")

    result.legal_name = (data.get("entityName") or data.get("name") or "").strip() or None
    result.entity_type = (data.get("entityType") or data.get("type") or "").strip() or None
    result.status = ((data.get("status") or data.get("entityStatus") or "")
                     .strip().lower() or None)

    fd = data.get("formationDate") or data.get("incorporationDate") or data.get("filingDate")
    if isinstance(fd, str) and fd:
        # AZCC dates often "MM/DD/YYYY" or ISO — normalize to YYYY-MM-DD
        m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", fd)
        if m:
            result.filing_date = f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
        else:
            m2 = re.match(r"^(\d{4})-(\d{2})-(\d{2})", fd)
            if m2:
                result.filing_date = f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"

    pa = data.get("principalAddress") or data.get("principalOfficeAddress")
    if isinstance(pa, str):
        result.principal_address = pa.strip()[:300] or None
    elif isinstance(pa, dict):
        parts = [pa.get("street1"), pa.get("street2"), pa.get("city"),
                 pa.get("state"), pa.get("zip") or pa.get("postalCode")]
        joined = ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
        result.principal_address = joined[:300] or None

    agent = data.get("statutoryAgent") or data.get("registeredAgent") or {}
    if isinstance(agent, dict):
        result.registered_agent_name = (agent.get("name") or agent.get("agentName") or "").strip() or None
        addr = agent.get("address")
        if isinstance(addr, str):
            result.registered_agent_address = addr.strip()[:300] or None
        elif isinstance(addr, dict):
            parts = [addr.get("street1"), addr.get("street2"), addr.get("city"),
                     addr.get("state"), addr.get("zip") or addr.get("postalCode")]
            joined = ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
            result.registered_agent_address = joined[:300] or None

    # Officers / members / managers — capture address too
    officer_lists = []
    for key in ("officers", "members", "managers", "directors", "principals"):
        v = data.get(key)
        if isinstance(v, list):
            officer_lists.append((key, v))
    seen = set()
    for key, lst in officer_lists:
        for o in lst:
            if not isinstance(o, dict):
                continue
            nm = (o.get("name") or
                  " ".join(p for p in [o.get("firstName"), o.get("lastName")] if p)).strip()
            if not nm:
                continue
            ttl = (o.get("title") or o.get("role") or key.rstrip("s").title()).strip()
            sig = (nm.lower(), ttl.lower())
            if sig in seen:
                continue
            seen.add(sig)
            # Address can be string or {street1, city, state, zip}
            addr_val = o.get("address") or o.get("mailingAddress") or o.get("homeAddress")
            addr_str: Optional[str] = None
            if isinstance(addr_val, str):
                addr_str = addr_val.strip()[:300] or None
            elif isinstance(addr_val, dict):
                parts = [addr_val.get("street1"), addr_val.get("street2"),
                         addr_val.get("city"), addr_val.get("state"),
                         addr_val.get("zip") or addr_val.get("postalCode")]
                joined = ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
                addr_str = joined[:300] or None
            result.officers.append(SoSOfficer(name=nm, title=ttl, address=addr_str))

    # Last annual report — AZCC exposes annual report list under
    # several possible keys depending on entity type.
    annual_dates: list[str] = []
    for key in ("annualReports", "annualReportFilings", "filings"):
        v = data.get(key)
        if isinstance(v, list):
            for entry in v:
                if not isinstance(entry, dict):
                    continue
                # Filter to annual-report-type filings if we're using a
                # generic 'filings' list
                if key == "filings":
                    ftype = (entry.get("type") or entry.get("filingType") or "").lower()
                    if "annual" not in ftype:
                        continue
                dt = (entry.get("filedDate") or entry.get("filingDate")
                      or entry.get("date") or entry.get("submitDate"))
                if isinstance(dt, str) and dt:
                    annual_dates.append(dt)
    if annual_dates:
        # Normalize each, keep the latest
        normalized: list[str] = []
        for raw in annual_dates:
            m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", raw)
            if m:
                normalized.append(f"{m.group(3)}-{m.group(1)}-{m.group(2)}")
                continue
            m2 = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
            if m2:
                normalized.append(m2.group(0))
        if normalized:
            result.last_annual_report_date = max(normalized)

    # DBAs / trade names — AZCC sometimes exposes these under
    # 'tradeNames' or 'doingBusinessAs'.
    for key in ("tradeNames", "doingBusinessAs", "dbaNames"):
        v = data.get(key)
        if isinstance(v, list):
            for entry in v:
                if isinstance(entry, str) and entry.strip():
                    result.dba_names.append(entry.strip()[:200])
                elif isinstance(entry, dict):
                    nm = (entry.get("name") or entry.get("tradeName") or "").strip()
                    if nm:
                        result.dba_names.append(nm[:200])

    result.years_in_business = _derive_years_in_business(result.filing_date)
    return result


# ============================================================
# Nevada — SilverFlume (https://esos.nv.gov)
# ============================================================
#
# Nevada SoS uses an ASP.NET WebForms search page at
# https://esos.nv.gov/EntitySearch/OnlineEntitySearch. Submitting the
# search form requires re-posting the __VIEWSTATE / __EVENTVALIDATION
# tokens we got from the initial GET. Detail pages are linked from
# the result row. Best-effort parser; conservative on anomalies.

NV_SEARCH_URL = "https://esos.nv.gov/EntitySearch/OnlineEntitySearch"


def _extract_aspnet_state(html: str) -> dict:
    """Pull __VIEWSTATE / __VIEWSTATEGENERATOR / __EVENTVALIDATION from
    an ASP.NET WebForms page. Missing tokens → empty dict."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION", "__EVENTTARGET", "__EVENTARGUMENT"):
        el = soup.select_one(f"input[name='{name}']")
        if el and el.get("value") is not None:
            out[name] = el["value"]
    return out


async def _lookup_nevada_uncached(company_name: str) -> SoSResult:
    result = SoSResult(state="NV")
    try:
        if not (company_name or "").strip():
            result.error = "empty_name"
            return result
        async with httpx.AsyncClient(
            timeout=25, follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            r = await client.get(NV_SEARCH_URL)
            if r.status_code != 200:
                result.error = f"search_get_http_{r.status_code}"
                return result

            state_fields = _extract_aspnet_state(r.text)
            if "__VIEWSTATE" not in state_fields:
                result.error = "no_viewstate"
                log.info(f"NV SoS missing __VIEWSTATE on initial GET")
                return result

            # Inspect the page to find the search-input + submit-button
            # element names. ASP.NET prefixes nested controls (e.g.
            # ctl00$BodyContentPlaceHolder$txtEntityName), so we don't
            # hard-code names — find by best-guess label proximity.
            soup = BeautifulSoup(r.text, "html.parser")
            text_inputs = soup.select("input[type='text']")
            entity_input_name = None
            for inp in text_inputs:
                nm = inp.get("name") or ""
                idv = (inp.get("id") or "").lower()
                if "entity" in idv or "name" in idv or "search" in idv.lower():
                    entity_input_name = nm
                    break
            # Fall back to first text input
            if not entity_input_name and text_inputs:
                entity_input_name = text_inputs[0].get("name")
            if not entity_input_name:
                result.error = "no_entity_input"
                return result

            submit_name = None
            for btn in soup.select("input[type='submit'], button[type='submit']"):
                nm = btn.get("name") or ""
                if "search" in (btn.get("value") or "").lower() or "search" in nm.lower():
                    submit_name = nm
                    break

            form_data = {**state_fields, entity_input_name: company_name.strip()}
            if submit_name:
                form_data[submit_name] = "Search"

            await asyncio.sleep(1.0)
            r2 = await client.post(
                NV_SEARCH_URL, data=form_data,
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "Referer": NV_SEARCH_URL},
            )
            if r2.status_code != 200:
                result.error = f"search_post_http_{r2.status_code}"
                return result

            soup2 = BeautifulSoup(r2.text, "html.parser")

            # Find the result table — typically id contains 'Results' or
            # 'gvEntity' or similar.
            rows = soup2.select("table tbody tr")
            if not rows:
                if "no records" in r2.text.lower() or "no entities" in r2.text.lower():
                    return result  # found=False, no error
                result.error = "no_result_rows"
                return result

            # First row that has a detail-page link
            link_row = None
            for row in rows[:5]:
                a = row.select_one("a[href*='BusinessEntityDetail'], a[href*='EntityInformation']")
                if a:
                    link_row = (row, a)
                    break
            if not link_row:
                result.error = "no_detail_link"
                return result

            row, a = link_row

            # Conservative match check: tokens overlap with query
            row_text = row.get_text(" ", strip=True)
            if not _name_token_overlap(row_text, company_name):
                result.error = "no_close_match"
                log.info(f"NV SoS top match row {row_text[:80]!r} far from query {company_name!r}")
                return result

            href = a.get("href") or ""
            detail_url = href if href.startswith("http") else f"https://esos.nv.gov{href if href.startswith('/') else '/EntitySearch/' + href}"

            await asyncio.sleep(1.0)
            r3 = await client.get(detail_url)
            if r3.status_code != 200:
                result.error = f"detail_http_{r3.status_code}"
                return result

            return _parse_nevada_detail(r3.text, detail_url)
    except httpx.HTTPError as e:
        log.warning(f"Nevada SoS network error for {company_name}: {e}")
        result.error = f"network: {e}"
        return result
    except Exception as e:
        log.warning(f"Nevada SoS lookup failed for {company_name}: {e}")
        result.error = f"exception: {e}"
        return result


def _parse_nevada_detail(html: str, url: str) -> SoSResult:
    """Parse a SilverFlume detail page. Pages render labeled fields in
    a series of <span> / <td> pairs. We use text-based label matching
    (same approach as FL) — tolerant of layout drift."""
    soup = BeautifulSoup(html, "html.parser")
    result = SoSResult(state="NV", found=True, raw_url=url)

    text = soup.get_text("\n", strip=True)

    # Legal name — usually under "Entity Name" or in a heading
    nm = re.search(r"Entity Name[:\s]*\n+([A-Z0-9 ,&'.\-/]+)", text)
    if nm:
        result.legal_name = nm.group(1).strip().rstrip(",")

    # Entity number — labeled "Entity Number" or "NV Business ID"
    en = re.search(r"(?:Entity Number|NV Business ID|Business ID)[:\s]*\n+([A-Z0-9\-]+)", text)
    if en:
        result.document_number = en.group(1).strip()

    # Entity type
    et = re.search(r"Entity Type[:\s]*\n+([A-Za-z ]+)", text)
    if et:
        result.entity_type = et.group(1).strip()

    # Filing date — labeled "Formation Date", "Date Filed", etc.
    fd = re.search(r"(?:Formation Date|Date Filed|Filing Date)[:\s]*\n+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})", text)
    if fd:
        m, d, y = fd.group(1).split("/")
        result.filing_date = f"{y}-{int(m):02d}-{int(d):02d}"

    # Status
    st = re.search(r"(?:Entity Status|Status)[:\s]*\n+([A-Za-z ]+)", text)
    if st:
        result.status = st.group(1).strip().lower()

    # Registered Agent
    ra = re.search(r"(?:Registered Agent Name|Registered Agent)[:\s]*\n+(.{0,400}?)(?:Agent Type|Agent Address|Officers|Managers|Members|Annual List)", text, re.S)
    if ra:
        block = [line.strip() for line in ra.group(1).splitlines() if line.strip()]
        if block:
            result.registered_agent_name = block[0][:120]
            result.registered_agent_address = " ".join(block[1:])[:300] or None

    # Principal Address
    pa = re.search(r"(?:Principal Address|Mailing Address)[:\s]*\n+(.{0,400}?)(?:Officers|Managers|Members|Registered Agent|Annual List)", text, re.S)
    if pa:
        result.principal_address = " ".join(line.strip() for line in pa.group(1).splitlines() if line.strip())[:300]

    # Officers / Managers / Members — capture address too. NV pages
    # typically render: "Title: <Title>\nName: <Name>\nAddress 1: <line>\n
    # Address 2: <line>\nCity, State, Zip: <line>".
    for label in ("Officers", "Managers", "Members"):
        sec = re.search(
            rf"{label}[:\s]*\n+(.{{0,3000}}?)(?:\n\s*(?:Officers|Managers|Members|Annual List|Stock|\Z))",
            text, re.S,
        )
        if not sec:
            continue
        block = sec.group(1)
        # Each officer block: title + name + (optional) address lines
        for m in re.finditer(
            r"(?:Title|Position)[:\s]+([A-Za-z /]+)\s*\n+"
            r"(?:Name[:\s]+)?([A-Z][A-Za-z, .\-']+)"
            r"(?:\s*\n+((?:Address[^\n]*\n+|City[^\n]*\n+|[0-9][^\n]*\n+){0,4}))?",
            block,
        ):
            ttl = m.group(1).strip()
            nm = m.group(2).strip().rstrip(",")
            addr_block = m.group(3) or ""
            if not nm:
                continue
            # Strip "Address 1:" / "City, State, Zip:" labels
            addr_lines: list[str] = []
            for raw in addr_block.splitlines():
                ln = raw.strip()
                if not ln:
                    continue
                ln = re.sub(r"^(?:Address\s*\d*|City,?\s*State,?\s*Zip)[:\s]+", "", ln, flags=re.I)
                if ln:
                    addr_lines.append(ln)
            address = ", ".join(addr_lines[:3])[:300] or None
            result.officers.append(SoSOfficer(name=nm, title=ttl or label.rstrip("s"), address=address))

    # Last annual report — NV pages have an "Annual List" section
    # listing recent annual filings with dates.
    al = re.search(r"Annual List[^\n]*\n+(.{0,2000}?)(?:Stock|Officers|Managers|Members|\Z)", text, re.S)
    if al:
        dates = re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", al.group(1))
        if dates:
            parsed = sorted({(int(y), int(m), int(d)) for m, d, y in dates}, reverse=True)
            y, m, d = parsed[0]
            result.last_annual_report_date = f"{y:04d}-{m:02d}-{d:02d}"

    # DBAs — NV maintains separate "trade name" filings, not usually on
    # entity detail pages. Left empty for now; standalone DBA search
    # can ship as a follow-up if useful.

    result.years_in_business = _derive_years_in_business(result.filing_date)
    return result


# ============================================================
# Shared helpers
# ============================================================

def _name_token_overlap(candidate: str, query: str, threshold: float = 0.34) -> bool:
    """Small Jaccard-style sanity check used to reject obviously-wrong
    top matches (same defensive posture that protected against the
    Proficient Patios cross-record bug). Threshold is intentionally
    permissive — we'd rather skip enrichment than corrupt data."""
    a = set(_normalize_name(candidate).split())
    b = set(_normalize_name(query).split())
    if not a or not b:
        return False
    overlap = len(a & b) / max(1, min(len(a), len(b)))
    return overlap >= threshold


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
    _log_field_coverage("FL", result)
    try:
        await _save_cache(db, "FL", company_name, result)
    except Exception:
        log.warning(f"Could not cache FL SoS result for {company_name}")
    return result


async def lookup_arizona(db: AsyncSession, company_name: str) -> SoSResult:
    cached = await _get_cached(db, "AZ", company_name)
    if cached is not None:
        return cached
    result = await _lookup_arizona_uncached(company_name)
    _log_field_coverage("AZ", result)
    try:
        await _save_cache(db, "AZ", company_name, result)
    except Exception:
        log.warning(f"Could not cache AZ SoS result for {company_name}")
    return result


async def lookup_nevada(db: AsyncSession, company_name: str) -> SoSResult:
    cached = await _get_cached(db, "NV", company_name)
    if cached is not None:
        return cached
    result = await _lookup_nevada_uncached(company_name)
    _log_field_coverage("NV", result)
    try:
        await _save_cache(db, "NV", company_name, result)
    except Exception:
        log.warning(f"Could not cache NV SoS result for {company_name}")
    return result


# Dispatcher — picks the right state scraper.
async def lookup_state(db: AsyncSession, state: Optional[str], company_name: str) -> Optional[SoSResult]:
    """Returns None when we don't have a scraper for that state yet
    (callers can ignore SoS in that case)."""
    if not state or not company_name:
        return None
    state_upper = (state or "").strip().upper()
    if state_upper in ("FL", "FLORIDA"):
        return await lookup_florida(db, company_name)
    if state_upper in ("AZ", "ARIZONA"):
        return await lookup_arizona(db, company_name)
    if state_upper in ("NV", "NEVADA"):
        return await lookup_nevada(db, company_name)
    # TX, CA, etc. — pending scraper implementations
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
