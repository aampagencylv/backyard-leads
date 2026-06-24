"""
Renders the standardized email signature for a User.

Org brand (logo, accent colors, company name, website URL) is pulled
live from RuntimeConfig via `get_org_brand`, so every signature shows
whatever the admin set in Settings → Org Brand. Per-user fields
(name, phone, email, scheduling URL) come from the User row.

scheduling_url falls back to the org-level iClosed booking URL when a
user hasn't set their own — so every BDR's signature has a 'Schedule a
discovery call' link out of the box without per-user setup.
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.config import settings

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


def effective_scheduling_url(user: User) -> str:
    """Per-user URL if set, else the BMP org iClosed booking URL.
    Returns empty string only when both are missing."""
    return (user.scheduling_url or "").strip() or (settings.iclosed_booking_url or "").strip()


# NEUTRAL last-resort fallback — used only when the brand resolver fails
# entirely. It must NOT be BMP-specific: render_signature overrides these
# with the tenant's RuntimeConfig values, but only when a value is truthy,
# so a tenant that legitimately leaves (say) website_url blank would
# otherwise inherit whatever sits here. Keeping it neutral means a clean
# tenant never ships another company's name/logo/site in its signature.
_DEFAULT_BRAND = {
    "primary_color":   "#2563EB",
    "secondary_color": "#1F2937",
    "accent_bg_color": "#F8FAFC",
    "logo_url":        "",
    "company_name":    "",
    "website_url":     "",
}


def _strip_scheme(url: str) -> str:
    """Render the website URL as 'www.example.com' rather than the full
    https:// for display purposes — matches the legacy signature's
    treatment of www.backyardmarketingpros.com."""
    s = (url or "").strip()
    if s.lower().startswith("https://"):
        s = s[8:]
    elif s.lower().startswith("http://"):
        s = s[7:]
    return s.rstrip("/")


async def render_signature(db: AsyncSession, user: User) -> str:
    """Async-aware signature renderer. Pulls org brand from RuntimeConfig
    so colors / logo / company name / website all reflect whatever the
    admin configured."""
    brand = dict(_DEFAULT_BRAND)
    try:
        from app.runtime_config import get_org_brand
        resolved = await get_org_brand(db)
        for k, v in resolved.items():
            if v:
                brand[k] = v
    except Exception:
        # Bootstrap path / migration mid-flight — fall back to BMP defaults
        # so the email still goes out instead of failing.
        pass

    # Resolve which calendar this user's "Schedule a discovery call" link
    # should target. If admin pointed them at a team calendar, use that.
    try:
        from app.services.booking_host import resolve_booking_url
        scheduling_url = await resolve_booking_url(db, user)
    except Exception:
        scheduling_url = effective_scheduling_url(user)

    template = _env.get_template("email_signature.html")
    return template.render(
        user={
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "nickname": user.nickname or "",
            "phone_number": user.phone_number or "",
            "email": user.email or "",
            "scheduling_url": scheduling_url,
        },
        brand={
            "primary_color":   brand["primary_color"],
            "secondary_color": brand["secondary_color"],
            "accent_bg_color": brand["accent_bg_color"],
            "logo_url":        brand["logo_url"] or _DEFAULT_BRAND["logo_url"],
            "company_name":    brand["company_name"],
            "website_url":     brand["website_url"],
            "website_display": _strip_scheme(brand["website_url"]),
        },
    )
