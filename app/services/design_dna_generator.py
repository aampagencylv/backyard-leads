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


PHOTO_CURATION_SYSTEM_PROMPT = """\
You are a photo editor curating images for a single-page website
preview. The site has three photo slots:

  - HERO: full-bleed background at the top. Wants wide, cinematic,
    establishing shot. The viewer's first impression. Prefer outdoor,
    wide-angle, finished-work shots. Avoid close-up product shots,
    people-only portraits, or interior-only photos for outdoor builders.
  - ABOUT: ~50% width, shown next to text. Wants portrait or context —
    a team shot, a craftsperson at work, a quality detail. Vertical or
    square framing works best.
  - GALLERY: 6 thumbnails in a grid. Pick 6 that together SHOW THE
    RANGE OF THEIR WORK. Avoid duplicates of the same project.

You will be shown a numbered list of candidate photos. Look at each
one and assign indices to each slot.

CRITICAL:
  - Return indices from the provided list ONLY. The first photo is
    index 0, the second is 1, and so on.
  - Hero and About should be different photos.
  - Gallery is an ORDERED list of 6 indices. They will render in this
    order in a grid.
  - If fewer than 8 photos are provided, repeat indices is fine
    (gallery can include the same photo twice in worst case).
  - Return ONLY JSON, no fences, no prose.

Output shape:
{
  "hero_idx": <int>,
  "hero_rationale": "<1 short sentence>",
  "about_idx": <int>,
  "about_rationale": "<1 short sentence>",
  "gallery_idx": [<int>, <int>, <int>, <int>, <int>, <int>]
}
"""


async def curate_photos(
    *,
    business_name: str,
    business_type: Optional[str],
    candidate_urls: list[str],
) -> Optional[dict]:
    """Have Claude (with vision) pick the best hero/about/gallery
    photos from a pool of candidates. Returns the curation dict or
    None on failure (caller falls back to [0]/[1]/[2:8])."""
    if not candidate_urls:
        return None
    # Cap at 10 images per request to keep Claude vision fast + cheap
    candidates = candidate_urls[:10]

    user_text = (
        f"BUSINESS: {business_name} ({business_type or 'unknown type'})\n\n"
        f"Below are {len(candidates)} candidate photos in numbered order. "
        "Look at each one and pick the best assignments for the three slots.\n\n"
        + "\n".join(f"  {i}: {u}" for i, u in enumerate(candidates))
        + "\n\nReturn the JSON now."
    )

    try:
        raw = await chat_with_vision(
            model=MODEL_BALANCED,
            system=PHOTO_CURATION_SYSTEM_PROMPT,
            user_text=user_text,
            image_urls=candidates,
            max_tokens=400,
            cacheable=True,
        )
    except Exception as e:
        log.warning(f"Photo curation call failed: {e}")
        return None

    txt = raw.strip()
    if "```json" in txt:
        txt = txt.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in txt:
        txt = txt.split("```", 1)[1].split("```", 1)[0]
    try:
        choice = json.loads(txt.strip())
    except json.JSONDecodeError:
        log.warning(f"Photo curation JSON parse failed; raw: {raw[:200]}")
        return None

    # Validate + clamp indices to the candidate pool
    n = len(candidates)
    def _safe_idx(i, default):
        try:
            i = int(i)
            if 0 <= i < n:
                return i
        except Exception:
            pass
        # Clamp the DEFAULT into range too — without this, gallery fallback
        # 'j' (enumeration position 0..5) can exceed pool size n when n<6,
        # causing candidates[i] to raise IndexError. Code-review #4.
        if n <= 0:
            return 0
        return max(0, min(default, n - 1))

    hero_idx = _safe_idx(choice.get("hero_idx"), 0)
    about_idx = _safe_idx(choice.get("about_idx"), 1 if n > 1 else 0)
    gallery_raw = choice.get("gallery_idx") or list(range(2, min(8, n)))
    gallery = [_safe_idx(i, j) for j, i in enumerate(gallery_raw[:6])]

    return {
        "hero_idx": hero_idx,
        "hero_url": candidates[hero_idx],
        "hero_rationale": choice.get("hero_rationale", ""),
        "about_idx": about_idx,
        "about_url": candidates[about_idx],
        "about_rationale": choice.get("about_rationale", ""),
        "gallery_idx": gallery,
        "gallery_urls": [candidates[i] for i in gallery],
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
