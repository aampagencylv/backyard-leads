"""Seed country_dialing_config for the NANP (+1) countries and Central America.

Companion to the area-code fix in twilio_voice._NANP_AREA_CODES. Before that
fix every +1 number resolved to "US"; now Bahamas/Jamaica/Cayman/etc. resolve
to their real ISO codes. international_dialing.check_call_allowed() REFUSES a
call to any country with no row here ("No compliance rules found for BS"), so
without this seed the fix would trade one opaque failure for another.

Calling windows are 8am-9pm local. recording_consent_required is set TRUE for
every non-US-mainland destination as the conservative default — it only emits
a warning to the rep, never blocks, and over-warning is the safe direction for
a jurisdiction we haven't researched individually.

Idempotent: INSERT ... ON CONFLICT (iso_country) DO NOTHING, so re-running
never overwrites a tenant-tuned row.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine

# (iso, name, tz, recording_consent, notes)
_ROWS = [
    # --- NANP Caribbean / Atlantic ---
    ("BS", "Bahamas",                     "America/Nassau",         True,  "NANP +1-242. Twilio geo permission is separate and off by default."),
    ("JM", "Jamaica",                     "America/Jamaica",        True,  "NANP +1-876/658."),
    ("DO", "Dominican Republic",          "America/Santo_Domingo",  True,  "NANP +1-809/829/849."),
    ("TT", "Trinidad and Tobago",         "America/Port_of_Spain",  True,  "NANP +1-868."),
    ("BB", "Barbados",                    "America/Barbados",       True,  "NANP +1-246."),
    ("KY", "Cayman Islands",              "America/Cayman",         True,  "NANP +1-345."),
    ("AG", "Antigua and Barbuda",         "America/Antigua",        True,  "NANP +1-268."),
    ("LC", "Saint Lucia",                 "America/St_Lucia",       True,  "NANP +1-758."),
    ("GD", "Grenada",                     "America/Grenada",        True,  "NANP +1-473."),
    ("VC", "St Vincent and Grenadines",   "America/St_Vincent",     True,  "NANP +1-784."),
    ("KN", "Saint Kitts and Nevis",       "America/St_Kitts",       True,  "NANP +1-869."),
    ("DM", "Dominica",                    "America/Dominica",       True,  "NANP +1-767."),
    ("TC", "Turks and Caicos Islands",    "America/Grand_Turk",     True,  "NANP +1-649."),
    ("AI", "Anguilla",                    "America/Anguilla",       True,  "NANP +1-264."),
    ("BM", "Bermuda",                     "Atlantic/Bermuda",       True,  "NANP +1-441."),
    ("MS", "Montserrat",                  "America/Montserrat",     True,  "NANP +1-664."),
    ("SX", "Sint Maarten",                "America/Lower_Princes",  True,  "NANP +1-721."),
    ("VG", "British Virgin Islands",      "America/Tortola",        True,  "NANP +1-284."),
    # --- US territories (NANP, US federal law, but separate Twilio destinations) ---
    ("PR", "Puerto Rico",                 "America/Puerto_Rico",    True,  "US territory, NANP +1-787/939. Twilio still gates it separately from US."),
    ("VI", "U.S. Virgin Islands",         "America/St_Thomas",      True,  "US territory, NANP +1-340."),
    ("GU", "Guam",                        "Pacific/Guam",           True,  "US territory, NANP +1-671."),
    ("MP", "Northern Mariana Islands",    "Pacific/Saipan",         True,  "US territory, NANP +1-670."),
    ("AS", "American Samoa",              "Pacific/Pago_Pago",      True,  "US territory, NANP +1-684."),
    # --- Canada (NANP) ---
    ("CA", "Canada",                      "America/Toronto",        True,  "NANP. Multi-timezone: window uses Eastern as the reference."),
    # --- Central America ---
    ("BZ", "Belize",                      "America/Belize",         True,  ""),
    ("CR", "Costa Rica",                  "America/Costa_Rica",     True,  ""),
    ("PA", "Panama",                      "America/Panama",         True,  ""),
    ("GT", "Guatemala",                   "America/Guatemala",      True,  ""),
    ("SV", "El Salvador",                 "America/El_Salvador",    True,  ""),
    ("HN", "Honduras",                    "America/Tegucigalpa",    True,  ""),
    ("NI", "Nicaragua",                   "America/Managua",        True,  ""),
]


async def main():
    async with engine.begin() as conn:
        exists = bool((await conn.execute(text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='country_dialing_config')"
        ))).scalar())
        if not exists:
            print("! country_dialing_config missing — run migrate_international_dialing first")
            return

        added = 0
        for iso, name, tz, consent, notes in _ROWS:
            res = await conn.execute(text("""
                INSERT INTO country_dialing_config
                    (iso_country, country_name, calling_window_start, calling_window_end,
                     default_timezone, caller_id_verification_required,
                     recording_consent_required, requires_local_number_registration,
                     compliance_notes)
                VALUES (:iso, :name, 8, 21, :tz, FALSE, :consent, FALSE, :notes)
                ON CONFLICT (iso_country) DO NOTHING
            """), {"iso": iso, "name": name, "tz": tz, "consent": consent, "notes": notes})
            if getattr(res, "rowcount", 0):
                added += 1

        total = (await conn.execute(text("SELECT COUNT(*) FROM country_dialing_config"))).scalar()
        print(f"+ added {added} country row(s); {len(_ROWS) - added} already present")
        print(f"✓ country_dialing_config now has {total} countries")


if __name__ == "__main__":
    asyncio.run(main())
