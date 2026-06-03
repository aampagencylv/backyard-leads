"""Brand asset extractor — Google Places photos + site scrape.

The Web Preview generator's quality lives or dies on the photos. Every
preview generated with the same two Unsplash placeholders looks like
generic AI slop regardless of how good the copy is. This module's job
is to pull REAL photos for every company:

  1. **Google Places photos** — pulled from the prospect's Google Maps
     listing via Place Details API. Most home-service businesses have
     5-30 photos on their GMB profile, all real work. Best source.
  2. **Site-scraped images** — pulled from the prospect's homepage via
     a lightweight HTTP fetch + BeautifulSoup. Captures hero images,
     gallery thumbnails, project photos.
  3. **Logo + brand color** — the favicon / og:image / apple-touch-icon
     give us the logo. Color-thief on the logo gives us the dominant
     brand color, which we inject as a CSS variable override so the
     preview feels like THEIR brand, not the template's defaults.

Cached in `companies.brand_assets_json` with a 30-day TTL. Refresh on
demand via `ensure_brand_assets(db, company, force=True)`.

All extraction is best-effort — every step is wrapped in a broad except
so a single failed scrape doesn't block preview generation. We just end
up with fewer assets, not no preview.
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Company
from app.runtime_config import get_google_maps_api_key

log = logging.getLogger("bmp.brand_extractor")

# Refresh cached assets after 30 days. Photos + branding don't change
# often; a monthly refresh is plenty for our use case.
CACHE_TTL_DAYS = 30

# Cap how many Places photos we keep — Google Places returns photo
# references one-at-a-time and each photo URL embeds the API key, so
# rendering 30 photos in a preview means 30 quota hits per page load.
# 12 photos = generous gallery + slack for hero/about selection.
MAX_PLACES_PHOTOS = 12

# Site-scrape: cap so a single <img> spam page doesn't fill our cache.
MAX_SITE_IMAGES = 20


# ============================================================
# Google Places photos
# ============================================================

async def resolve_google_place_id(name: str, city: str, state: str, api_key: str) -> Optional[str]:
    """Find the real Google place_id via Find Place From Text API.

    Important: companies.google_place_id stores Netrows feature_ids (0x...),
    NOT Google native place_ids (ChIJ...). Photo API needs the latter.
    We resolve on demand + cache in brand_assets_json so it's a one-time
    cost per company.

    Returns ChIJ-format place_id or None.
    """
    if not (name and api_key):
        return None
    query = " ".join(p for p in [name, city, state] if p).strip()
    url = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
    params = {
        "input": query,
        "inputtype": "textquery",
        "fields": "place_id,name",
        "key": api_key,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params=params)
            data = r.json() or {}
            candidates = data.get("candidates") or []
            if candidates:
                pid = candidates[0].get("place_id")
                if pid and pid.startswith("ChIJ"):
                    return pid
    except Exception as e:
        log.warning(f"resolve_google_place_id failed for '{query}': {e}")
    return None


async def _places_details(place_id: str, api_key: str) -> dict:
    """Call Place Details API requesting photos field. Returns the raw
    JSON `result` dict, or {} on failure."""
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": "photos,name,formatted_address",
        "key": api_key,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params=params)
            data = r.json()
            return data.get("result", {}) or {}
    except Exception as e:
        log.warning(f"Places details failed for {place_id}: {e}")
        return {}


def _places_photo_url(photo_reference: str, api_key: str, max_width: int = 1600) -> str:
    """Build the URL that resolves to the actual photo binary. The URL
    is self-contained (includes the API key) so we can embed it in the
    rendered HTML."""
    return (
        "https://maps.googleapis.com/maps/api/place/photo"
        f"?maxwidth={max_width}&photo_reference={photo_reference}&key={api_key}"
    )


async def fetch_google_places_photos(
    company: Company, api_key: str, *, cached_place_id: Optional[str] = None,
) -> tuple[list[str], Optional[str]]:
    """Returns (photo URLs, resolved Google native place_id).

    Tries cached_place_id first, then resolves via Find Place From Text
    using business name + city + state. The resolved place_id is returned
    so the caller can stash it in brand_assets_json for next time.
    """
    if not api_key:
        return [], cached_place_id

    pid = cached_place_id if cached_place_id and cached_place_id.startswith("ChIJ") else None
    if not pid:
        pid = await resolve_google_place_id(
            company.name or "", company.city or "", company.state or "", api_key
        )
    if not pid:
        return [], None

    details = await _places_details(pid, api_key)
    photos = (details.get("photos") or [])[:MAX_PLACES_PHOTOS]
    urls = []
    for p in photos:
        ref = p.get("photo_reference")
        if not ref:
            continue
        urls.append(_places_photo_url(ref, api_key))
    return urls, pid


# ============================================================
# Site scrape — logo + images + brand color
# ============================================================

# Image filename patterns we strip out: stock placeholders, icons,
# tiny pixels, social-media glyphs, CSS sprites.
_IMG_NOISE_PATTERNS = [
    r"/sprite", r"/icon", r"icons?\d*\.png$",
    r"social[-_]", r"facebook", r"twitter", r"instagram\.svg",
    r"pixel\.gif$", r"1x1\.", r"transparent\.png$",
    r"/wp-includes/", r"/wp-admin/",
    r"\.gif(\?|$)",  # most .gifs are tracking pixels or social icons
]


def _looks_like_real_image(url: str, alt: str) -> bool:
    """Filter out obvious noise — sprites, social icons, tracking pixels."""
    if not url:
        return False
    u = url.lower()
    if any(re.search(p, u) for p in _IMG_NOISE_PATTERNS):
        return False
    # Strip query strings before extension check
    path = u.split("?", 1)[0]
    if not any(path.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".avif")):
        return False
    return True


def _absolutize(base_url: str, src: str) -> str:
    """Turn a relative or protocol-relative img src into an absolute URL."""
    if not src:
        return ""
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith(("http://", "https://")):
        return src
    return urljoin(base_url, src)


async def _fetch_homepage(url: str) -> Optional[str]:
    """GET the company's homepage HTML. Returns None on failure.

    Uses a real-browser UA — small-business sites commonly 403 anything
    that identifies as a bot. We're not crawling — one polite request
    per company per month — so the real-browser UA is honest enough.
    """
    if not url:
        return None
    # Normalize: ensure scheme
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
            r = await client.get(url)
            if r.status_code >= 400:
                log.warning(f"Homepage fetch returned {r.status_code} for {url}")
                return None
            return r.text
    except Exception as e:
        log.warning(f"Homepage fetch failed for {url}: {type(e).__name__}: {e}")
        return None


def _extract_logo_and_images(html: str, base_url: str) -> tuple[Optional[str], list[dict]]:
    """Parse the homepage HTML to find the best logo candidate + a
    deduped list of substantive page images. Returns (logo_url, images)."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("beautifulsoup4 not installed — falling back to regex")
        return _extract_logo_and_images_regex(html, base_url)

    soup = BeautifulSoup(html, "html.parser")

    # Logo candidates in priority order:
    #   1. <link rel="apple-touch-icon"> — usually the highest-res square
    #   2. og:image meta tag
    #   3. <img> with 'logo' in src/class/id/alt
    #   4. <link rel="icon"> (favicon)
    logo_url = None
    for sel, attr in [
        ('link[rel="apple-touch-icon"]', "href"),
        ('link[rel="apple-touch-icon-precomposed"]', "href"),
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
    ]:
        el = soup.select_one(sel)
        if el and el.get(attr):
            logo_url = _absolutize(base_url, el.get(attr))
            break
    if not logo_url:
        for img in soup.find_all("img"):
            blob = " ".join(
                str(img.get(k) or "") for k in ("src", "class", "id", "alt")
            ).lower()
            if "logo" in blob:
                src = img.get("src")
                if src:
                    logo_url = _absolutize(base_url, src)
                    break
    if not logo_url:
        for sel in ('link[rel="icon"]', 'link[rel="shortcut icon"]'):
            el = soup.select_one(sel)
            if el and el.get("href"):
                logo_url = _absolutize(base_url, el.get("href"))
                break

    # All substantive page images
    seen = set()
    images: list[dict] = []
    for img in soup.find_all("img"):
        src = _absolutize(base_url, img.get("src", ""))
        alt = (img.get("alt") or "").strip()
        if not _looks_like_real_image(src, alt):
            continue
        # Dedupe by URL path (strip query) so the same photo served with
        # different width params only appears once.
        key = src.split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        images.append({"url": src, "alt": alt})
        if len(images) >= MAX_SITE_IMAGES:
            break

    return logo_url, images


def _extract_logo_and_images_regex(html: str, base_url: str) -> tuple[Optional[str], list[dict]]:
    """Fallback extractor that doesn't require bs4. Less accurate."""
    # og:image
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html, re.I)
    logo_url = _absolutize(base_url, m.group(1)) if m else None
    # img src list
    imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>', html, re.I)
    seen = set()
    images = []
    for src in imgs:
        absurl = _absolutize(base_url, src)
        if not _looks_like_real_image(absurl, ""):
            continue
        key = absurl.split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        images.append({"url": absurl, "alt": ""})
        if len(images) >= MAX_SITE_IMAGES:
            break
    return logo_url, images


# ============================================================
# Brand color
# ============================================================

async def _dominant_color_from_logo(logo_url: str) -> Optional[str]:
    """Download the logo + return the dominant non-white/non-black hex.

    Uses Pillow if available. If the logo URL is unreachable or Pillow
    isn't installed, returns None and the preview falls back to the
    template's default palette. Best-effort by design.
    """
    if not logo_url:
        return None
    try:
        from PIL import Image
        from io import BytesIO
    except ImportError:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            r = await client.get(logo_url)
            if r.status_code >= 400 or not r.content:
                return None
            img = Image.open(BytesIO(r.content)).convert("RGB")
            img.thumbnail((128, 128))  # downsample for speed
    except Exception as e:
        log.warning(f"Logo color extraction failed: {e}")
        return None

    # Quantize to 8 colors, take the most common that isn't near-white,
    # near-black, or near-gray. Returns a hex string.
    try:
        q = img.quantize(colors=8, method=Image.MEDIANCUT)
        palette = q.getpalette()[:8 * 3]
        counts = sorted(q.getcolors() or [], key=lambda c: -c[0])
        for count, idx in counts:
            r, g, b = palette[idx * 3], palette[idx * 3 + 1], palette[idx * 3 + 2]
            if _is_chromatic(r, g, b):
                return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        pass
    return None


def _is_chromatic(r: int, g: int, b: int) -> bool:
    """True if the color isn't near-white, near-black, or near-gray.
    We want the BRAND color, not the background or text color."""
    # Near-white / near-black
    if max(r, g, b) > 240 and min(r, g, b) > 220:
        return False
    if max(r, g, b) < 40:
        return False
    # Near-gray (small spread between channels)
    if max(r, g, b) - min(r, g, b) < 25:
        return False
    return True


# ============================================================
# Orchestrator
# ============================================================

def _is_stale(payload: dict) -> bool:
    """True if the cached assets are missing or older than TTL."""
    ts = payload.get("fetched_at")
    if not ts:
        return True
    try:
        fetched = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - fetched) > timedelta(days=CACHE_TTL_DAYS)
    except Exception:
        return True


async def ensure_brand_assets(
    db: AsyncSession,
    company: Company,
    *,
    force: bool = False,
    persist: bool = True,
) -> dict:
    """Fetch + cache brand assets for a company. Returns the asset dict:

        {
          "google_photos": [...],
          "site_images": [...],
          "site_logo_url": "...",
          "site_brand_color": "#hex",
          "fetched_at": "..."
        }

    Reads the existing cache unless `force=True` or it's stale.
    Set persist=False to compute without writing (e.g. dry-run preview).
    """
    # Cache hit?
    existing: dict = {}
    if company.brand_assets_json:
        try:
            existing = json.loads(company.brand_assets_json)
        except Exception:
            existing = {}
    if existing and not force and not _is_stale(existing):
        return existing

    # Cold path — fetch everything in parallel where possible.
    api_key = await get_google_maps_api_key(db)
    cached_native_pid = (existing or {}).get("google_native_place_id")

    places_task = asyncio.create_task(
        fetch_google_places_photos(company, api_key, cached_place_id=cached_native_pid)
    )
    homepage_task = asyncio.create_task(_fetch_homepage(company.website or ""))

    google_photos, resolved_pid = await places_task
    homepage_html = await homepage_task

    site_logo_url: Optional[str] = None
    site_images: list[dict] = []
    if homepage_html and company.website:
        site_logo_url, site_images = _extract_logo_and_images(homepage_html, company.website)

    site_brand_color = await _dominant_color_from_logo(site_logo_url) if site_logo_url else None

    payload = {
        "google_photos": google_photos,
        "google_native_place_id": resolved_pid,
        "site_images": site_images,
        "site_logo_url": site_logo_url,
        "site_brand_color": site_brand_color,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if persist:
        company.brand_assets_json = json.dumps(payload)
        await db.flush()
    return payload
