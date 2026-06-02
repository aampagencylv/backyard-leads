"""
Twilio sub-account provisioning for new tenants.

Twilio's master-account API supports creating sub-accounts via
  POST https://api.twilio.com/2010-04-01/Accounts.json
with Basic auth using the master account's SID + auth token.

A sub-account is a fully-isolated Twilio account:
  - Its own SID + auth token
  - Its own phone numbers + verified caller IDs
  - Its own A2P 10DLC brand + campaign registration
  - Separate billing line items (rolls up to the master for invoicing)

That isolation is exactly what we need for multi-tenant. On tenant
create we provision one, store the SID + auth token in the tenant's
RuntimeConfig, and every downstream Twilio call (voice / SMS / webhooks)
already reads those via the ORM auto-filter — no further refactor needed.

Configuration:
  TWILIO_MASTER_ACCOUNT_SID   — env var, the master account
  TWILIO_MASTER_AUTH_TOKEN    — env var

When either is empty we skip provisioning silently — the tenant gets
created with no sub-account; an operator can attach one later in the
admin console.
"""
from __future__ import annotations
import logging
import os
from typing import Optional, Tuple

import httpx

log = logging.getLogger("bmp.twilio_provisioning")


def _master_creds() -> Optional[Tuple[str, str]]:
    """Return (sid, token) for the master Twilio account, or None if
    not configured. We intentionally read directly from env (not the
    settings module) so this can be turned on without an app restart
    by setting the systemd EnvironmentFile."""
    sid = (os.environ.get("TWILIO_MASTER_ACCOUNT_SID") or "").strip()
    token = (os.environ.get("TWILIO_MASTER_AUTH_TOKEN") or "").strip()
    if not sid or not token:
        return None
    return sid, token


def is_configured() -> bool:
    """True when the master credentials are available. Used by callers
    to decide whether to surface a "provisioning not configured" warning."""
    return _master_creds() is not None


async def create_sub_account(friendly_name: str) -> Optional[Tuple[str, str]]:
    """Create a Twilio sub-account. Returns (sub_sid, sub_auth_token).

    Returns None (and logs a warning) when:
      - The master credentials aren't configured (silently skip — caller
        decides what to do)
      - The Twilio API returns a non-2xx (we don't want to fail tenant
        creation just because the side-effect failed; the operator can
        retry from the admin console)

    Never raises. Tenant creation is the primary action; provisioning
    is opportunistic.
    """
    creds = _master_creds()
    if not creds:
        log.info("twilio sub-account skipped — TWILIO_MASTER_* not set")
        return None

    sid, token = creds
    url = f"https://api.twilio.com/2010-04-01/Accounts.json"
    payload = {"FriendlyName": friendly_name[:64]}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, auth=(sid, token), data=payload)
        if r.status_code >= 400:
            log.warning(f"twilio sub-account create failed: {r.status_code} {r.text[:200]}")
            return None
        data = r.json()
        sub_sid = data.get("sid")
        sub_token = data.get("auth_token")
        if not sub_sid or not sub_token:
            log.warning(f"twilio response missing sid/auth_token: {data}")
            return None
        log.info(f"twilio sub-account provisioned: {sub_sid} ({friendly_name})")
        return sub_sid, sub_token
    except Exception:
        log.exception("twilio sub-account provisioning raised; ignoring")
        return None
