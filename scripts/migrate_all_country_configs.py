"""Seed country_dialing_config for EVERY dialling region in the world.

Reps dial wherever their market is, so the reference table has to cover the
world rather than a curated shortlist. Partial coverage was a live failure
mode: `check_call_allowed` used to refuse any country with no row, which
looked to the rep like a carrier problem.

Generated from `phonenumbers` (Google's libphonenumber, 245 regions) plus
`pytz.country_timezones` for a representative timezone per country. Both are
real dependencies now, pinned in requirements.txt.

Defaults per country:
  - calling window 8am-9pm LOCAL time
  - recording_consent_required = TRUE. Conservative on purpose: it only emits
    a warning to the rep, never blocks, and over-warning is the safe direction
    for ~200 jurisdictions nobody has researched individually.
  - caller_id_verification_required / requires_local_number_registration =
    FALSE, since those are specific programmes (e.g. Mexico's COFETEL) that
    have to be set per country deliberately.

Idempotent: ON CONFLICT DO NOTHING, so hand-tuned rows (US, MX, the NANP
set) are never overwritten. Safe to re-run after a phonenumbers upgrade to
pick up newly added regions.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine

# Multi-timezone countries where pytz's first entry is a poor stand-in for
# "where most people answer the phone" (it returns Australia/Lord_Howe for AU,
# an island of ~350 people). Calling windows are approximate for any country
# spanning several zones; these just make the approximation sane.
_TZ_OVERRIDES = {
    "US": "America/New_York",
    "CA": "America/Toronto",
    "AU": "Australia/Sydney",
    "RU": "Europe/Moscow",
    "BR": "America/Sao_Paulo",
    "MX": "America/Mexico_City",
    "CN": "Asia/Shanghai",
    "IN": "Asia/Kolkata",
    "ID": "Asia/Jakarta",
    "KZ": "Asia/Almaty",
    "CD": "Africa/Kinshasa",
    "ES": "Europe/Madrid",
    "PT": "Europe/Lisbon",
    "CL": "America/Santiago",
    "EC": "America/Guayaquil",
    "AR": "America/Argentina/Buenos_Aires",
    "GL": "America/Nuuk",
    "NZ": "Pacific/Auckland",
    "MN": "Asia/Ulaanbaatar",
    "PF": "Pacific/Tahiti",
    "KI": "Pacific/Tarawa",
    "FM": "Pacific/Pohnpei",
    "UA": "Europe/Kyiv",
}

# Regions phonenumbers knows but pytz has no timezone for.
_TZ_FALLBACKS = {
    "AC": "Atlantic/St_Helena",   # Ascension Island
    "TA": "Atlantic/St_Helena",   # Tristan da Cunha
    "XK": "Europe/Belgrade",      # Kosovo
}


def _build_rows():
    import phonenumbers
    import pytz
    from phonenumbers import phonenumberutil as pnu

    try:
        from phonenumbers.geocoder import _region_display_name  # type: ignore
    except Exception:
        _region_display_name = None

    rows = []
    for region in sorted(phonenumbers.SUPPORTED_REGIONS):
        tz = _TZ_OVERRIDES.get(region)
        if not tz:
            zones = pytz.country_timezones.get(region) or []
            tz = zones[0] if zones else _TZ_FALLBACKS.get(region)
        if not tz:
            tz = "UTC"

        name = None
        if _region_display_name:
            try:
                name = _region_display_name(region, "en")
            except Exception:
                name = None
        if not name:
            try:
                name = pytz.country_names.get(region)
            except Exception:
                name = None
        name = (name or region)[:100]

        try:
            cc = pnu.country_code_for_region(region)
        except Exception:
            cc = 0

        rows.append((region, name, tz, f"+{cc}" if cc else ""))
    return rows


async def main():
    rows = _build_rows()
    async with engine.begin() as conn:
        exists = bool((await conn.execute(text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='country_dialing_config')"
        ))).scalar())
        if not exists:
            print("! country_dialing_config missing — run migrate_international_dialing first")
            return

        added = 0
        for iso, name, tz, cc in rows:
            res = await conn.execute(text("""
                INSERT INTO country_dialing_config
                    (iso_country, country_name, calling_window_start, calling_window_end,
                     default_timezone, caller_id_verification_required,
                     recording_consent_required, requires_local_number_registration,
                     compliance_notes)
                VALUES (:iso, :name, 8, 21, :tz, FALSE, TRUE, FALSE, :notes)
                ON CONFLICT (iso_country) DO NOTHING
            """), {
                "iso": iso, "name": name, "tz": tz,
                "notes": f"Auto-seeded from libphonenumber. Dial code {cc}. "
                         f"Calling window is a default, not researched per jurisdiction.",
            })
            if getattr(res, "rowcount", 0):
                added += 1

        total = (await conn.execute(text("SELECT COUNT(*) FROM country_dialing_config"))).scalar()
        print(f"+ added {added} country row(s); {len(rows) - added} already present")
        print(f"✓ country_dialing_config now covers {total} countries")


if __name__ == "__main__":
    asyncio.run(main())
