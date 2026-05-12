"""
IP → Company resolution for the website visitor identification feature.

Today this is a thin wrapper around IPInfo Lite (free tier, 1k req/day)
that returns ASN + reverse-DNS + geo. For each request we:

  1. Check for private/local IPs and short-circuit (no point).
  2. GET https://ipinfo.io/{ip}/json with timeout.
  3. Parse `org` ("AS#### Org Name") to get the ASN-registered company.
  4. Compare against a baked-in ISP heuristic list — when the org name
     matches a known consumer ISP, flag is_isp_ip=True so the UI can
     filter these out (they're noise for B2B intent).
  5. Try reverse-DNS hostname to get a corp domain (often missing).

Return shape:
    {
      "domain": Optional[str],         # corp-looking domain if we found one
      "company_name": Optional[str],   # org name from ASN registration
      "country": Optional[str],
      "region": Optional[str],
      "city": Optional[str],
      "is_isp_ip": bool,
    }

Or None when the lookup failed entirely.

Designed for swap: when Steve upgrades to a real B2B reveal API
(Clearbit/RB2B/Apollo/Demandbase), drop in a new resolver function and
flip an env var. Same return shape.
"""
from __future__ import annotations
import ipaddress
import logging
import os
import re
from typing import Optional
import httpx

log = logging.getLogger("bmp.visitor_resolver")

IPINFO_URL = "https://ipinfo.io/{ip}/json"
IPINFO_TIMEOUT = 4.0

# Consumer ISP org names — when an IP's ASN org matches one of these,
# it's an ISP IP, not a business IP. The match is substring-insensitive.
ISP_KEYWORDS = [
    "comcast", "spectrum", "charter", "cox communications", "verizon",
    "centurylink", "lumen", "frontier", "windstream", "at&t internet",
    "at&t broadband", "att internet", "atlantic broadband", "wow! internet",
    "wave broadband", "midco", "cable one", "sparklight", "cablevision",
    "altice", "optimum", "rcn", "earthlink", "hughes network", "viasat",
    "starlink", "spacex", "tmobile usa", "t-mobile usa", "tmobile internet",
    "t-mobile internet", "sprint", "u.s. cellular", "us cellular", "boost mobile",
    "google fiber", "fios", "xfinity", "rogers", "shaw", "telus", "bell canada",
    "isp ", "broadband", "communications inc",
]

# Common AS_org -> domain map for major business networks where the
# org name doesn't include the domain. We don't try to be comprehensive
# — just enough to be useful for big tech visitors.
ORG_DOMAIN_HINTS = {
    "google llc": "google.com",
    "amazon.com inc": "amazon.com",
    "amazon technologies": "amazon.com",
    "microsoft corporation": "microsoft.com",
    "meta platforms inc": "meta.com",
    "facebook inc": "facebook.com",
    "apple inc": "apple.com",
    "oracle corporation": "oracle.com",
    "salesforce.com": "salesforce.com",
    "hubspot inc": "hubspot.com",
    "stripe inc": "stripe.com",
    "shopify inc": "shopify.com",
}


def _is_private_or_invalid(ip: str) -> bool:
    if not ip:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
        return True
    if addr.is_multicast:
        return True
    return False


def _looks_like_isp(org_name: str) -> bool:
    if not org_name:
        return False
    s = org_name.lower()
    return any(kw in s for kw in ISP_KEYWORDS)


def _domain_from_hostname(hostname: Optional[str]) -> Optional[str]:
    """Try to extract a useful corp domain from a reverse-DNS hostname.

    Examples that produce a result:
      ec2-3-25-44.compute.amazonaws.com   → amazonaws.com (we strip this; AWS isn't a customer)
      mailout.acme.com                    → acme.com
      cgw01.example.org                   → example.org

    Returns None for IP-looking hostnames or empty input."""
    if not hostname:
        return None
    h = hostname.strip().lower().rstrip(".")
    if not h or re.fullmatch(r"[\d.\-]+", h):
        return None
    # Reject the obvious cloud-provider reverse-DNS suffixes — they're
    # the provider's domain, not the customer's.
    JUNK_SUFFIXES = ("amazonaws.com", "googleusercontent.com", "compute.azure.com",
                     "azure.com", "googlebot.com", "msftncsi.com", "in-addr.arpa")
    for suf in JUNK_SUFFIXES:
        if h.endswith(suf):
            return None
    parts = h.split(".")
    if len(parts) < 2:
        return None
    # Take the last two parts as the registered domain (TLD-agnostic
    # rough approximation; good enough for the .com / .net / .io 95% case).
    return ".".join(parts[-2:])


def _parse_org(raw: str) -> tuple[Optional[str], Optional[str]]:
    """IPInfo returns 'AS15169 Google LLC' style org strings. Returns
    (asn, org_name)."""
    if not raw:
        return None, None
    m = re.match(r"^(AS\d+)\s+(.+)$", raw.strip())
    if m:
        return m.group(1), m.group(2).strip()
    return None, raw.strip()


async def resolve_ip(ip: str, *, ipinfo_token: Optional[str] = None) -> Optional[dict]:
    """Best-effort IP→company. Returns None on private/invalid IPs +
    on lookup failure."""
    if _is_private_or_invalid(ip):
        return None

    headers = {"Accept": "application/json"}
    params = {}
    token = ipinfo_token or os.environ.get("IPINFO_TOKEN") or ""
    if token:
        params["token"] = token

    try:
        async with httpx.AsyncClient(timeout=IPINFO_TIMEOUT) as client:
            r = await client.get(IPINFO_URL.format(ip=ip), params=params, headers=headers)
            if r.status_code != 200:
                log.warning(f"ipinfo {r.status_code} for {ip}: {r.text[:120]}")
                return None
            data = r.json()
    except Exception as e:
        log.warning(f"ipinfo lookup failed for {ip}: {e}")
        return None

    org_raw = (data.get("org") or "").strip()
    _, org_name = _parse_org(org_raw)
    hostname = (data.get("hostname") or "").strip().lower() or None
    domain = _domain_from_hostname(hostname)

    # Last-ditch: org_name might literally contain the domain
    if not domain and org_name:
        m = re.search(r"\b([a-z0-9\-]+\.(?:com|net|org|io|ai|co|app|inc))\b", org_name.lower())
        if m:
            domain = m.group(1)

    # Big-tech bypass: well-known org names get a baked-in domain
    if not domain and org_name:
        key = org_name.lower().strip(".,")
        if key in ORG_DOMAIN_HINTS:
            domain = ORG_DOMAIN_HINTS[key]

    is_isp_ip = _looks_like_isp(org_name or "")

    return {
        "domain": domain,
        "company_name": (org_name or None) if not is_isp_ip else None,
        "country": (data.get("country") or "").strip().upper() or None,
        "region": (data.get("region") or "").strip() or None,
        "city": (data.get("city") or "").strip() or None,
        "is_isp_ip": is_isp_ip,
        "hostname": hostname,
        "raw_org": org_raw,
    }
