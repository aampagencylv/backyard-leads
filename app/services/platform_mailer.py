"""
Platform-level email sender (LeadProspector → user, not tenant → prospect).

Wraps the LeadProspector Resend account (separate from any tenant's
Resend setup). Used for system emails:
  - user invites when a new tenant user is provisioned
  - password reset
  - onboarding nudges (tenant abandoned setup)
  - platform-admin notifications

Tenant prospect outreach does NOT go through here — that uses the
tenant's own Resend domain via the existing send_routes pipeline. This
module is strictly for emails the platform itself originates.

Configuration (VPS env):
  PLATFORM_RESEND_API_KEY        — the LeadProspector Resend account key
                                   (different from RESEND_API_KEY which
                                   is BMP-tenant's account)
  PLATFORM_SENDING_DOMAIN        — the verified domain to send from
                                   (e.g. system.leadprospector.ai)
  PLATFORM_FROM_NAME             — display name, default "LeadProspector"
  PLATFORM_LOGIN_URL_BASE        — base URL for login links, default
                                   "https://app.leadprospector.ai"

When PLATFORM_RESEND_API_KEY is unset, `send_platform_email` becomes
a structured no-op: logs the intended send, returns a sentinel, never
raises. This means commit-and-deploy now is safe even before Steve
creates the Resend account.
"""
from __future__ import annotations
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("bmp.platform_mailer")


# ----------------------------------------------------------------------
# Template strings
# ----------------------------------------------------------------------
#
# Plain dicts so we can iterate later — no Jinja or external template
# engine. Variables substituted via str.format(**vars).
#
# Keep each template's subject < 50 chars, body < ~400 words, plain
# HTML so it renders in every client.

_TEMPLATES: dict[str, dict[str, str]] = {
    "user_invite": {
        "subject": "You've been invited to {tenant_name} on LeadProspector",
        "body_html": """<p>Hi {first_name},</p>
<p>{actor_name} added you to <strong>{tenant_name}</strong> on LeadProspector.</p>
<p>Your sign-in details:</p>
<ul>
  <li><strong>URL:</strong> <a href="{login_url}">{login_url}</a></li>
  <li><strong>Email:</strong> {email}</li>
  <li><strong>Temp password:</strong> <code>{temp_password}</code></li>
</ul>
<p>Sign in and change your password from Settings on first login.</p>
<p style="color:#888;font-size:12px;margin-top:24px">— The LeadProspector team</p>""",
        "body_text": """Hi {first_name},

{actor_name} added you to {tenant_name} on LeadProspector.

Sign-in details:
  URL:       {login_url}
  Email:     {email}
  Password:  {temp_password}

Sign in and change your password from Settings on first login.

— The LeadProspector team
""",
    },
    "password_reset": {
        "subject": "Reset your LeadProspector password",
        "body_html": """<p>Hi {first_name},</p>
<p>Click the link below to reset your LeadProspector password. The link
expires in 30 minutes.</p>
<p><a href="{reset_url}">Reset password</a></p>
<p>If you didn't request this, you can safely ignore this email.</p>
<p style="color:#888;font-size:12px;margin-top:24px">— The LeadProspector team</p>""",
        "body_text": """Hi {first_name},

Reset your LeadProspector password (link expires in 30 min):
  {reset_url}

If you didn't request this, ignore this email.

— The LeadProspector team
""",
    },
    "onboarding_nudge": {
        "subject": "Finish setting up {tenant_name} on LeadProspector",
        "body_html": """<p>Hi {first_name},</p>
<p>You started setting up <strong>{tenant_name}</strong> on LeadProspector
but didn't finish. Picking up where you left off takes about 2 minutes.</p>
<p><a href="{login_url}">Continue setup</a></p>
<p>Need help? Just reply to this email — it goes straight to us.</p>
<p style="color:#888;font-size:12px;margin-top:24px">— The LeadProspector team</p>""",
        "body_text": """Hi {first_name},

You started setting up {tenant_name} on LeadProspector but didn't finish.
Picking up where you left off takes about 2 minutes:

  {login_url}

Need help? Just reply to this email — it goes straight to us.

— The LeadProspector team
""",
    },
}


# ----------------------------------------------------------------------
# Sender
# ----------------------------------------------------------------------

def _config() -> Optional[dict]:
    """Read platform Resend config from env. Returns None if not set.

    Sending domain defaults to `leadprospector.ai` (apex) since the
    LeadProspector Resend workspace verifies the apex directly — system
    emails are low-volume notifications (invites, password resets) and
    don't need a dedicated subdomain for deliverability isolation.
    """
    key = (os.environ.get("PLATFORM_RESEND_API_KEY") or "").strip()
    if not key:
        return None
    return {
        "api_key": key,
        "domain": (os.environ.get("PLATFORM_SENDING_DOMAIN") or "leadprospector.ai").strip(),
        "from_name": (os.environ.get("PLATFORM_FROM_NAME") or "LeadProspector").strip(),
        "from_local": (os.environ.get("PLATFORM_FROM_LOCAL") or "hello").strip(),
        "login_url": (os.environ.get("PLATFORM_LOGIN_URL_BASE") or "https://app.leadprospector.ai").strip(),
    }


def is_configured() -> bool:
    return _config() is not None


async def send_platform_email(
    *,
    to: str,
    template: str,
    vars: Optional[dict] = None,
) -> Optional[str]:
    """Send a system email via the platform Resend account.

    Returns the Resend message id on success, None when not configured
    or on transient failure. Never raises — caller treats None as
    "best-effort delivery failed" and decides whether to retry.

    `template` must be a key in `_TEMPLATES`. `vars` substitutes into
    the subject + body via str.format(**vars).
    """
    cfg = _config()
    if not cfg:
        log.info(f"platform email skipped — PLATFORM_RESEND_API_KEY unset; would have sent template={template} to={to}")
        return None

    t = _TEMPLATES.get(template)
    if not t:
        log.error(f"unknown platform email template: {template}")
        return None

    v = dict(vars or {})
    try:
        subject = t["subject"].format(**v)
        body_html = t["body_html"].format(**v)
        body_text = t["body_text"].format(**v)
    except KeyError as e:
        log.error(f"missing template var for {template}: {e}")
        return None

    from_address = f"{cfg['from_name']} <{cfg['from_local']}@{cfg['domain']}>"
    payload = {
        "from": from_address,
        "to": [to],
        "subject": subject,
        "html": body_html,
        "text": body_text,
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post("https://api.resend.com/emails", json=payload, headers=headers)
        if r.status_code >= 400:
            log.warning(f"platform email send failed {r.status_code}: {r.text[:240]}")
            return None
        data = r.json()
        msg_id = data.get("id") or data.get("data", {}).get("id")
        log.info(f"platform email sent: id={msg_id} template={template} to={to}")
        return msg_id
    except Exception:
        log.exception("platform email send raised; returning None")
        return None
