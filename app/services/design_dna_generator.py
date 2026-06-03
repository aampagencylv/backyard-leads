"""Lightweight per-prospect design DNA — colors, logo, font pairing.

NOT a full brand style guide. Three things only:
  1. The prospect's actual brand color (extracted via Pillow OR refined
     by Claude looking at the logo)
  2. The prospect's actual logo (extracted via site scrape)
  3. A Google Font pairing that resonates with their brand vibe
     (chosen by Claude via vision — this is the new piece)

Claude's vision lets us pick a font that fits the brand instead of
forcing every preview into Fraunces+Inter regardless of vertical. A
craft pool builder gets a confident-warm serif pairing; an HVAC trade
gets a no-nonsense single-family; a med spa gets old-world didone.

The template.html stays mostly the same — its CSS variables consume
these values. Everything else (layout, spacing, voice rules) still
comes from the template's static design.md.

Cached in brand_assets_json.design_dna with the 30-day TTL.
Costs ~$0.015-0.03 per prospect on Sonnet w/ vision.
"""
from __future__ import annotations
import json
import logging
from typing import Optional

from app.services.ai_client import chat_with_vision, MODEL_BALANCED

log = logging.getLogger("bmp.design_dna_generator")

# Curated Google Font pairings. Claude picks ONE id from this list — no
# hallucination risk, every option has been validated to load + look
# good together. Vibe descriptions help Claude match brand to pairing.
GOOGLE_FONT_PAIRINGS = [
    {
        "id": "fraunces_inter",
        "display": "Fraunces",
        "body": "Inter",
        "vibe": "Confident-warm. Modern serif with optical sizes paired with the workhorse sans. Good for craft trades, premium home services, modern outdoor builders.",
        "google_url": "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Inter:wght@400;500;600&display=swap",
    },
    {
        "id": "playfair_lato",
        "display": "Playfair Display",
        "body": "Lato",
        "vibe": "Elegant-traditional. High-contrast serif with humanist sans. Good for luxury, real estate, fine dining, weddings, established law firms.",
        "google_url": "https://fonts.googleapis.com/css2?family=Playfair+Display:wght@500;600&family=Lato:wght@400;700&display=swap",
    },
    {
        "id": "dmsans_dmserif",
        "display": "DM Serif Display",
        "body": "DM Sans",
        "vibe": "Editorial-clean. Big confident serif over a geometric sans. Good for design studios, agencies, modern boutiques, contemporary B2B.",
        "google_url": "https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=DM+Sans:wght@400;500;700&display=swap",
    },
    {
        "id": "spacegrotesk_inter",
        "display": "Space Grotesk",
        "body": "Inter",
        "vibe": "Modern-tech. Geometric display + workhorse sans. Good for SaaS, fintech, contemporary trades that want to feel current.",
        "google_url": "https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&display=swap",
    },
    {
        "id": "manrope_only",
        "display": "Manrope",
        "body": "Manrope",
        "vibe": "Single-family minimalist. No-nonsense. Good for HVAC, plumbing, electrical, locksmith — the utility trades.",
        "google_url": "https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap",
    },
    {
        "id": "lora_source",
        "display": "Lora",
        "body": "Source Sans 3",
        "vibe": "Approachable-editorial. Calligraphic serif + neutral sans. Good for wellness, salons, organic/natural brands, family heritage businesses.",
        "google_url": "https://fonts.googleapis.com/css2?family=Lora:wght@500;600;700&family=Source+Sans+3:wght@400;500;600&display=swap",
    },
    {
        "id": "archivo_archivo",
        "display": "Archivo Black",
        "body": "Archivo",
        "vibe": "Bold-utility. Heavy block display + clean grotesk body. Good for emergency trades, towing, restoration, anything urgent.",
        "google_url": "https://fonts.googleapis.com/css2?family=Archivo+Black&family=Archivo:wght@400;500;600&display=swap",
    },
    {
        "id": "cormorant_inter",
        "display": "Cormorant Garamond",
        "body": "Inter",
        "vibe": "Old-world luxury. High-contrast didone serif + sans body. Good for med spas, fine jewelry, plastic surgery, classic luxury services.",
        "google_url": "https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Inter:wght@400;500;600&display=swap",
    },
]


def _pairing_by_id(pid: str) -> dict:
    """Lookup with fallback — defensive against Claude hallucinating an id."""
    for p in GOOGLE_FONT_PAIRINGS:
        if p["id"] == pid:
            return p
    return GOOGLE_FONT_PAIRINGS[0]


def _darken_hex(hex_color: str, amount: float = 0.25) -> str:
    """Return a darker shade of the input hex for hover states.
    amount=0.25 means 25% darker; values from 0.0-0.6 are useful.
    Robust against bad input — returns the input unchanged on error."""
    try:
        h = hex_color.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        r = max(0, int(r * (1 - amount)))
        g = max(0, int(g * (1 - amount)))
        b = max(0, int(b * (1 - amount)))
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return hex_color


DNA_SYSTEM_PROMPT = """\
You pick a Google Font pairing that fits a brand. Nothing else.

You will see a logo image (sometimes) and business context (name, type,
location). Choose ONE font pairing id from the provided list whose vibe
description matches what this brand FEELS like.

Look at the logo: is it modernist or classical? Heavy or refined? Warm
or cool? Trade-utility or premium-craft? Match the pairing's vibe to
the brand's vibe.

Examples of correct matching:
  - Pool builder in Sedona with handcrafted serif logo → fraunces_inter
  - 24/7 emergency HVAC with bold sans logo → manrope_only or archivo_archivo
  - Med spa with elegant didone wordmark → cormorant_inter
  - Modern landscape design studio in Brooklyn → dmsans_dmserif

CRITICAL:
  - Pick from the EXACT ids provided — do not invent.
  - Return ONLY a JSON object, no fences, no prose.

Output shape:
{
  "pairing_id": "<one of the provided ids>",
  "rationale": "1 short sentence explaining the match in plain language"
}
"""


async def generate_design_dna(
    *,
    company_name: str,
    business_type: Optional[str],
    city: Optional[str],
    state: Optional[str],
    logo_url: Optional[str],
    extracted_primary_color: Optional[str],
) -> Optional[dict]:
    """Pick a font pairing for this brand via Claude vision. Returns:

        {
          "primary_color":      "#hex",   // extracted_primary_color or None
          "primary_color_dark": "#hex",   // auto-darkened primary, for hover state
          "font_pairing_id":    "...",
          "font_display":       "...",
          "font_body":          "...",
          "font_google_url":    "...",
          "font_rationale":     "..."
        }

    Returns None on LLM failure — caller falls back to template defaults.
    """
    images: list[str] = [logo_url] if logo_url else []

    user_text = (
        f"BRAND:\n"
        f"  Name: {company_name}\n"
        f"  Type: {business_type or '(unknown)'}\n"
        f"  Location: {city or ''}{', ' + state if state else ''}\n"
        f"  Brand color (extracted, may be noisy): {extracted_primary_color or '(none)'}\n\n"
        f"AVAILABLE PAIRINGS:\n"
        + "\n".join(f"  - {p['id']}: {p['vibe']}" for p in GOOGLE_FONT_PAIRINGS) +
        ("\n\nThe image is their logo. Match it." if logo_url else
         "\n\nNo logo available — infer from business context.") +
        "\n\nReturn the JSON now."
    )

    try:
        raw = await chat_with_vision(
            model=MODEL_BALANCED,
            system=DNA_SYSTEM_PROMPT,
            user_text=user_text,
            image_urls=images,
            max_tokens=300,
            cacheable=True,
        )
    except Exception as e:
        log.warning(f"Design DNA call failed: {e}")
        return None

    txt = raw.strip()
    if "```json" in txt:
        txt = txt.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in txt:
        txt = txt.split("```", 1)[1].split("```", 1)[0]
    try:
        choice = json.loads(txt.strip())
    except json.JSONDecodeError:
        log.warning(f"Design DNA JSON parse failed; raw: {raw[:200]}")
        return None

    pairing = _pairing_by_id(choice.get("pairing_id", "fraunces_inter"))
    primary = extracted_primary_color or None
    primary_dark = _darken_hex(primary, 0.30) if primary else None

    return {
        "primary_color":      primary,
        "primary_color_dark": primary_dark,
        "font_pairing_id":    pairing["id"],
        "font_display":       pairing["display"],
        "font_body":          pairing["body"],
        "font_google_url":    pairing["google_url"],
        "font_rationale":     choice.get("rationale", ""),
    }


def fallback_design_dna() -> dict:
    """When DNA generation fails or no logo is available, fall back to
    template defaults so the preview still renders."""
    p = GOOGLE_FONT_PAIRINGS[0]
    return {
        "primary_color":      None,
        "primary_color_dark": None,
        "font_pairing_id":    p["id"],
        "font_display":       p["display"],
        "font_body":          p["body"],
        "font_google_url":    p["google_url"],
        "font_rationale":     "template default",
    }
