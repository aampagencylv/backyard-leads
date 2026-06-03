"""
Web preview generator.

Generates a single-page homepage preview for a prospect company, intended
to be sent in a cold email ("here's what your real site could look like").

Pipeline:
  1. Pick a template based on business_type (or rep override).
  2. Assemble data: company info from DB + scraped photos from Places.
  3. LLM fills structured slots (headline, services, about, CTA).
     System prompt = the template's design.md. Output = JSON.
     Prompt-cached so the design.md (~2-3K tokens) is a cache hit
     after the first generation per template.
  4. Jinja2 renders template.html with the slot data → final HTML.
  5. Caller persists to web_previews table + returns the URL.

The design.md acts as a brand contract: the visual layer is locked in
the pre-built template.html, and the LLM only produces compliant slot
copy. This avoids "AI slop" by separating design (us) from content (LLM).
"""
from __future__ import annotations
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.services.ai_client import chat_with_system, MODEL_BALANCED

log = logging.getLogger("bmp.web_preview_generator")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "web_preview_templates"

# Business-type → template slug mapping. Vertical-aware picking so
# pool builders get "Modern Outdoor", salons get "Luxury", etc.
# Future: more granular + LLM-assisted picking. For now keyword-match.
_VERTICAL_MAP: list[tuple[list[str], str]] = [
    # Outdoor / home-service: BMP's vertical + adjacent.
    (["pool", "landscap", "deck", "backyard", "outdoor kitchen",
      "patio", "hardscape", "fence", "lawn", "garden", "irrigation",
      "tree", "arborist", "concrete", "paver"], "modern_outdoor"),
    # Future templates land here as we ship them:
    # (["salon", "spa", "beauty", "med spa"], "luxury"),
    # (["hvac", "plumb", "electric", "roof"], "local_trust"),
]


def pick_template(business_type: Optional[str]) -> str:
    """Pick which template slug fits this business. Falls back to
    modern_outdoor (our only template at MVP)."""
    if not business_type:
        return "modern_outdoor"
    bt = business_type.lower()
    for keywords, slug in _VERTICAL_MAP:
        if any(kw in bt for kw in keywords):
            return slug
    return "modern_outdoor"


def slugify(name: str) -> str:
    """Company name → URL-safe slug. 'Bob's Pool Builders LLC' → 'bobs-pool-builders'."""
    s = (name or "").lower().strip()
    s = re.sub(r"\b(llc|inc|corp|ltd|co)\b\.?", "", s)
    s = re.sub(r"['’]", "", s)  # strip apostrophes
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60] or "preview"


def short_token(n: int = 4) -> str:
    """Random URL suffix to disambiguate same-name previews + block guessing."""
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"  # readable, no 0/o/1/l confusion
    return "".join(secrets.choice(alphabet) for _ in range(n))


# ----------------------------------------------------------------------
# Data assembly
# ----------------------------------------------------------------------

def _assemble_business_data(company) -> dict:
    """Pack the company row into the dict shape the LLM expects.

    Pulls everything we already know from the audit/crawl: name, type,
    location, services we inferred from their About scrape, problems_found,
    rating, review_count, etc.
    """
    return {
        "name": company.name,
        "business_type": company.business_type or "",
        "location_city": company.city or "",
        "location_state": company.state or "",
        "website": company.website or "",
        "phone": company.phone or "",
        "rating": float(company.rating or 0),
        "review_count": int(company.review_count or 0),
        "year_established": company.founded or None,
        "employee_count": company.employee_count or None,
        "company_description": (company.company_description or "")[:1500],
        "specialties": (company.specialties or "")[:500],
        "industry": company.industry or "",
        "enrichment_summary": (company.enrichment_summary or "")[:800],
    }


def _assemble_photo_data(company, places_photos: list[str], unsplash_fallback: list[str]) -> dict:
    """Pick the hero, about, and gallery photos.

    Preference order:
      1. Scraped photos from their actual site (best — real work)
      2. Google Places photos (good — public, professional)
      3. Unsplash by business type (last resort — generic but consistent)
    """
    photos = []
    # Existing site scrape (TODO: wire in once site_scrape stores image_urls)
    if hasattr(company, "image_urls_json") and company.image_urls_json:
        try:
            photos.extend(json.loads(company.image_urls_json)[:9])
        except Exception:
            pass
    # Google Places
    photos.extend(places_photos[:9 - len(photos)])
    # Unsplash fallback
    photos.extend(unsplash_fallback[:9 - len(photos)])

    photos = [p for p in photos if p]
    if not photos:
        # Last-ditch: a known-good Unsplash fallback so the preview never
        # renders with broken images.
        photos = [
            "https://images.unsplash.com/photo-1614632537423-1e6c2e7e0e8e?w=1600",
            "https://images.unsplash.com/photo-1568605114967-8130f3a36994?w=1200",
        ]

    return {
        "hero": photos[0],
        "about": photos[1] if len(photos) > 1 else photos[0],
        "gallery": photos[2:8] if len(photos) > 2 else [],
    }


# ----------------------------------------------------------------------
# LLM slot generation
# ----------------------------------------------------------------------

SLOT_INSTRUCTIONS = """\
You are writing the COPY for a single-page website preview that will be
sent to a prospect as part of a sales pitch. The visual design is
already built in code per the DESIGN SYSTEM above — your job is only
to produce the text content as structured JSON.

You will receive the prospect's business data. Output ONLY a JSON
object matching this exact schema:

{
  "hero": {
    "eyebrow": string,        // 2-4 words, e.g. "Phoenix's pool builders"
    "headline": string,       // 6-12 words, outcome the customer wants in their words
    "subhead": string,        // 18-32 words, supporting line — specific, no clichés
    "cta_text": string        // 3-5 words, e.g. "Start your project", "See if we're a fit"
  },
  "trust_strip": [             // exactly 3 stats
    { "value": string, "label": string }
    // value is short (number, "5.0", "150+"). label is 3-5 words UPPERCASE-OK
  ],
  "services": {
    "section_label": string,   // 2-3 words, e.g. "What we build"
    "section_headline": string,// 5-9 words
    "items": [                 // 3-6 services
      { "name": string,        // 1-3 words
        "description": string  // 15-28 words, plain language
      }
    ]
  },
  "about": {
    "eyebrow": string,         // 2-3 words
    "headline": string,        // 5-9 words
    "paragraphs": [string]     // 2-3 short paragraphs, 30-60 words each
  },
  "testimonial": {             // optional — omit field entirely if no review data
    "quote": string,           // 15-30 words, paraphrased from their reviews
    "author": string           // "Sarah M., Mesa" or "Verified Google review"
  },
  "cta_section": {
    "headline": string,        // 5-10 words
    "subhead": string,         // 12-22 words
    "button_text": string      // 3-5 words
  },
  "tagline": string            // 3-6 words for the header tagline
}

STRICT RULES:
- Use the prospect's actual business name, city, services, and any
  specific details from their data. Never invent factual claims.
- If a piece of data is missing (e.g. no year_established), do NOT
  fabricate a number. Use a soft alternative ("Family-run", "Local").
- Voice + tone + anti-patterns from the DESIGN SYSTEM above are
  non-negotiable. No "solutions", no "trusted", no "Why choose us".
- Every word the prospect will read should be specific to THEIR
  business. The prospect will know immediately if it's generic.
- For the testimonial: paraphrase from the spirit of their reviews +
  rating. If review_count is 0, OMIT the testimonial field.
- Return ONLY the JSON object. No markdown fences. No prose.
"""


async def _generate_slots(design_md: str, business_data: dict) -> dict:
    """Single LLM call. system = design.md + slot instructions; user =
    business data; output = parsed slot dict."""
    system = f"{design_md}\n\n---\n\n{SLOT_INSTRUCTIONS}"
    user = "BUSINESS DATA:\n```json\n" + json.dumps(business_data, indent=2) + "\n```"
    raw = await chat_with_system(
        model=MODEL_BALANCED,
        system=system,
        user=user,
        max_tokens=2400,
        cacheable=True,  # design.md is fixed → huge cache savings after 1st call
    )
    # Strip code fences if Claude adds them despite instructions
    txt = raw.strip()
    if "```json" in txt:
        txt = txt.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in txt:
        txt = txt.split("```", 1)[1].split("```", 1)[0]
    return json.loads(txt.strip())


# ----------------------------------------------------------------------
# Renderer
# ----------------------------------------------------------------------

def _render(template_slug: str, ctx: dict) -> str:
    """Jinja2 render template.html with the slot + photo + meta context."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR / template_slug)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("template.html")
    return template.render(**ctx)


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------

async def generate_web_preview(
    *,
    company,
    agency_name: str,
    agency_url: str,
    cta_url: str,
    places_photos: Optional[list[str]] = None,
    unsplash_fallback: Optional[list[str]] = None,
    template_override: Optional[str] = None,
) -> dict:
    """Generate a single-page web preview for `company`.

    Returns:
      {
        "template_slug": str,
        "slug": str,                    # URL slug — '{company-slug}-{token}'
        "html": str,                    # the rendered HTML
        "slots": dict,                  # the LLM's slot output (so we can edit later)
        "photos": dict,                 # which photos got used
        "cost_estimate_usd": float,
      }

    Never raises. Returns {"error": "..."} on failure (caller decides
    whether to surface or retry).
    """
    template_slug = template_override or pick_template(company.business_type)
    template_dir = TEMPLATES_DIR / template_slug
    design_md_path = template_dir / "design.md"
    if not design_md_path.exists():
        return {"error": f"template not found: {template_slug}"}
    design_md = design_md_path.read_text()

    business_data = _assemble_business_data(company)
    photos = _assemble_photo_data(company, places_photos or [], unsplash_fallback or [])

    try:
        slots = await _generate_slots(design_md, business_data)
    except Exception as e:
        log.exception("LLM slot generation failed")
        return {"error": f"LLM generation failed: {str(e)[:200]}"}

    # Build the render context. Everything the template references must
    # be here — the LLM controls copy, we control structure.
    ctx = {
        "business": {
            "name": company.name,
            "tagline": slots.get("tagline", ""),
            "location": f"{company.city or ''}{', ' + company.state if company.state else ''}".strip(", ") or "Local",
        },
        "hero":         slots.get("hero", {}),
        "trust_strip":  slots.get("trust_strip", []),
        "services":     slots.get("services", {}),
        "about":        slots.get("about", {}),
        "testimonial":  slots.get("testimonial"),
        "cta_section":  slots.get("cta_section", {}),
        "photos":       photos,
        "cta_url":      cta_url or "#",
        "agency": {
            "name": agency_name,
            "url": agency_url,
            "url_display": (agency_url or "").replace("https://", "").replace("http://", "").rstrip("/"),
        },
        "year": datetime.now(timezone.utc).year,
    }

    html = _render(template_slug, ctx)
    url_slug = f"{slugify(company.name)}-{short_token()}"

    return {
        "template_slug": template_slug,
        "slug": url_slug,
        "html": html,
        "slots": slots,
        "photos": photos,
        # Rough cost estimate — Sonnet w/ caching:
        # Input ~3K (mostly cached) + Output ~2K → ~$0.04-0.06
        "cost_estimate_usd": 0.05,
    }
