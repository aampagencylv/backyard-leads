"""
Email-validation hard gate.

Before any send (manual or sequence-engine), call ensure_email_validated()
to check whether the email is deliverable. If we haven't verified yet,
fire Hunter's /v2/email-verifier — costs $0.04/lookup, metered as
email_verify, cached on Contact.email_status so it's a one-time fee.

Outcomes:
  - 'valid'        — Hunter says deliverable; allow send
  - 'risky'        — catch-all / accept-all / role address; allow but mark
  - 'unknown'      — Hunter couldn't decide; allow but mark
  - 'invalid'      — Hunter says undeliverable; BLOCK send
  - 'bounced'      — set elsewhere on bounce webhook; BLOCK send

Fail-open policy: if Hunter is down or no API key, we allow the send
rather than block all outreach. Better to absorb a few bounces than to
silently halt every BDR's pipeline.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Contact

log = logging.getLogger("bmp.email_validation")

# email_status values that mean "skip the verify call — already known good"
ALREADY_VALIDATED = {"valid", "verified", "deliverable"}
# email_status values that mean "block — known bad"
BLOCKED = {"invalid", "undeliverable", "bounced"}


async def ensure_email_validated(
    db: AsyncSession,
    contact: Contact,
    force: bool = False,
) -> tuple[bool, str]:
    """Returns (ok_to_send, reason).

    ok_to_send=False ONLY when the email is known-bad. Verify failures,
    risky results, and unknown results all pass — better to ship a few
    risky sends than to halt the pipeline on an outage.
    """
    if not contact.email:
        return False, "no_email"

    # Already known-bad? Block.
    if (contact.email_status or "").lower() in BLOCKED:
        return False, f"email_status={contact.email_status}"

    # Already known-good and not forced? Skip the verify call.
    if not force and (contact.email_status or "").lower() in ALREADY_VALIDATED:
        return True, "cached_valid"

    # Need to verify. If we can't (no API key), fail open.
    if not settings.hunter_api_key:
        return True, "no_hunter_key_fail_open"

    try:
        from app.services.hunter_enrichment import verify_email
        result = await verify_email(contact.email, settings.hunter_api_key)
        hr = (result.get("result") or "").lower()
        score = result.get("score")
    except Exception as e:
        log.warning(f"verify_email crashed for {contact.email}: {e}")
        return True, "hunter_error_fail_open"

    # Map Hunter result to our cache vocabulary
    if hr == "undeliverable":
        contact.email_status = "invalid"
        ok = False
    elif hr in ("deliverable",):
        contact.email_status = "valid"
        ok = True
    elif hr == "risky":
        contact.email_status = "risky"
        ok = True
    else:  # 'unknown' or empty
        contact.email_status = "unknown"
        ok = True

    # Meter the call regardless of outcome — Hunter charged us either way.
    try:
        from app.services.credit_meter import meter, make_idem_key
        await meter(
            db, action_type="email_verify",
            idempotency_key=make_idem_key("email_verify", contact.id, datetime.now(timezone.utc).date().isoformat()),
            user_id=None, action_ref=f"contact:{contact.id}",
            metadata={"hunter_result": hr, "score": score},
        )
    except Exception:
        pass

    return ok, f"hunter_{hr or 'unknown'}"
