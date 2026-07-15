# International Dialing Configuration

This guide explains how to set up and use international calling in LeadProspector for Mexico, Latin America, and other countries.

## Architecture Overview

**Country Dialing Config** (reference table) stores compliance rules per country:
- Calling window (e.g., 8am-9pm local time)
- Caller ID verification required
- Recording consent required
- Local number registration requirements

**Tenant Config** specifies which countries each tenant can call (whitelist).

**Contact Phone Country** is inferred from the E.164 phone number when the contact is created.

Before initiating ANY outbound call, the system checks:
1. Is calling this country enabled for the tenant?
2. Is it within the calling window for that country?
3. Does the caller have proper verification/consent?

## Setup Steps

### 1. Run Migration

```bash
venv/bin/python scripts/migrate_international_dialing.py
```

This creates:
- `country_dialing_config` table (seeded with US, MX, BR, CO, CL, AR)
- `tenant_ai_config.supported_calling_countries_json` column
- `contacts.phone_country_code` column

### 2. Enable Countries for a Tenant

Use the admin API:

```bash
# View current config
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  https://prospector.backyardmarketingpros.com/api/admin/tenants/13/calling-config

# Enable MX + BR for tenant 13
curl -X PATCH -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"supported_calling_countries": ["US", "MX", "BR"]}' \
  https://prospector.backyardmarketingpros.com/api/admin/tenants/13/calling-config
```

### 3. Buy International Numbers

Once a country is enabled, admins can search/buy numbers:

```bash
# Search available MX numbers
curl -H "Authorization: Bearer $TOKEN" \
  'https://prospector.backyardmarketingpros.com/api/twilio/numbers/available?iso_country=MX&limit=10'

# Buy a number
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"phone_number": "+525551234567"}' \
  https://prospector.backyardmarketingpros.com/api/twilio/numbers/buy
```

### 4. Make Calls

When a rep initiates an outbound call to any number, the system automatically:
- Extracts the country code from the phone (e.g., +52 → MX)
- Checks if tenant is allowed to call that country
- Checks if it's within the calling window (8am-9pm local time)
- Logs warnings if compliance requirements apply (e.g., "Recording consent required in Mexico")

Calls are rejected with a TTS message if:
- Country is not enabled
- Outside calling window
- Missing critical compliance setup

## Compliance Notes by Country

### Mexico (+52)
- ✓ Caller ID verification required (COFETEL)
- ✓ Recording consent required (explicit consent must be obtained before recording)
- ✓ Local number registration required
- Calling window: 8am–9pm Mexico City time
- **Next steps**: Coordinate with Twilio for COFETEL registration + caller ID verification

### Brazil (+55)
- ✓ Caller ID verification required
- ✓ Recording consent required
- Calling window: 8am–9pm São Paulo time

### Colombia (+57), Chile (+56), Argentina (+54)
- ✓ Recording consent required
- Calling window: 8am–9pm local time

### US (+1)
- No verification required (default)
- TCPA calling window: 8am–9pm Eastern time
- (If B2B override enabled: no time restriction)

## How to Extend: Adding New Countries

Edit `scripts/migrate_international_dialing.py` or use SQL:

```sql
INSERT INTO country_dialing_config
  (iso_country, country_name, calling_window_start, calling_window_end, default_timezone,
   caller_id_verification_required, recording_consent_required, requires_local_number_registration,
   compliance_notes)
VALUES
  ('PE', 'Peru', 8, 21, 'America/Lima', TRUE, TRUE, FALSE, 'Recording consent required');
```

Then restart the app so the new country is available for tenant enrollment.

## API Reference

### Admin Endpoints

**GET /api/admin/country-dialing-config**
List all country compliance rules.

**GET /api/admin/tenants/{tenant_id}/calling-config**
Get current calling countries for a tenant.

**PATCH /api/admin/tenants/{tenant_id}/calling-config**
```json
{
  "supported_calling_countries": ["US", "MX", "BR"]
}
```
Set which countries a tenant can call.

### Dialer Endpoints

**GET /api/twilio/numbers/available**
Search available numbers in a country (only shows numbers in enabled countries).

Query params:
- `iso_country` (default: "US")
- `area_code` (optional, US only)
- `limit` (default: 10)

Rejects if country not enabled for tenant.

## Testing

```bash
# Test compliance check for a call to Mexico
# (as an internal function; not exposed via API yet)
from app.services.international_dialing import check_call_allowed
from datetime import datetime, timezone

compliance = await check_call_allowed(
  db,
  to_e164="+525551234567",  # Mexico number
  tenant_id=13,
  now_utc=datetime(2025, 7, 15, 18, 0, tzinfo=timezone.utc),  # 6 PM UTC
)
print(compliance.allowed)  # True (within 8am-9pm Mexico City time)
print(compliance.warnings)  # ["Caller ID verification required in Mexico", ...]
```

## Troubleshooting

**"Calling {COUNTRY} is not enabled for your account"**
→ Admin needs to enable the country for your tenant via PATCH /api/admin/tenants/{id}/calling-config

**"Outside calling window"**
→ The destination country's local time is outside 8am–9pm. Retry during allowed hours.

**"Recording consent required in {COUNTRY}"**
→ Twilio Voice SDK will emit a consent disclosure before recording. Ensure the rep confirms the caller's consent.

**"Caller ID verification required"**
→ The number you're calling from must be registered with local telecom authority (e.g., COFETEL for Mexico). Contact admin to verify the number.

## Future Enhancements

- [ ] Per-country SMS throughput limits
- [ ] Per-country DLR (delivery receipt) handling
- [ ] Automatic A2P 10DLC / short-code registration flow for new countries
- [ ] Per-contact recording consent flag (linked to contact record)
- [ ] Country override (allow US caller to explicitly consent to different recording laws)
