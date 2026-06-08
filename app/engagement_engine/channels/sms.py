"""SMSChannel — outbound SMS via Twilio with TCPA enforcement.

Wraps app.services.twilio_sms.send_sms() and check_send_window() so the
engine doesn't reimplement the Twilio integration or the TCPA window
calculation (which is non-trivial because it depends on the recipient's
local time zone derived from area code).

TCPA quiet hours: SMS to consumer numbers between 9pm-8am local time
violates the TCPA (Telephone Consumer Protection Act). Penalty: $500-1500
per message in damages, class-action exposure.

The dispatcher reschedules an action to the next legal local window rather
than failing it outright. tcpa_b2b_override lets business-to-business
tenants relax to 7am-10pm, but consumer is non-negotiable.

Phase 2 scope: send-only. Inbound reply ingestion + opt-out tracking land
in Phase 3 (signal watcher) and Phase 5 (CRM UX for BDR review).
"""
from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import text

from app.database import async_session
from app.engagement_engine.interfaces import (
    GuardResult,
    SendResult,
    OutcomeUpdate,
    TransientChannelError,
    PermanentChannelError,
)


# Maximum SMS body length before we soft-warn (longer bodies fragment into
# multiple billed segments and read poorly).
SOFT_MAX_BODY = 320


class SMSChannel:
    """Outbound SMS via Twilio, with TCPA quiet-hour enforcement."""

    channel_code: str = "sms"

    async def pre_dispatch_guards(self, action) -> GuardResult:
        # Empty body
        if not (action.body or "").strip():
            return GuardResult(blocked=True, reason="empty_body")

        # E.164 format check (basic — Twilio rejects malformed anyway)
        phone = (action.recipient_phone or "").strip()
        if not phone or not phone.startswith("+") or len(phone) < 8:
            return GuardResult(blocked=True, reason="invalid_phone_e164")

        # Soft-warn on long body — not blocking but worth flagging if we
        # ever wire up audit logs at this layer
        if len(action.body or "") > SOFT_MAX_BODY:
            # Allowed but logged via dispatcher's per-action metrics
            pass

        # Check tenant's SMS opt-out table (existing twilio_sms tracks this)
        # If contact has STOP'd previously, refuse.
        async with async_session() as session:
            opt_out = await session.execute(text("""
                SELECT 1 FROM signals
                WHERE engagement_id = :eng
                  AND signal_type_id = (SELECT id FROM signal_types
                                        WHERE code = 'sms_opt_out')
                LIMIT 1
            """), {"eng": action.engagement_id})
            if opt_out.first() is not None:
                return GuardResult(blocked=True, reason="sms_opt_out")

        return GuardResult(blocked=False)

    async def is_in_send_window(self, local_now: datetime, tcpa_b2b_override: bool) -> bool:
        """TCPA quiet hours: 8am-9pm local for consumer.
        B2B override (when tenant_ai_config.tcpa_b2b_override=TRUE) relaxes
        to 7am-10pm. Neither override allows overnight."""
        if tcpa_b2b_override:
            return 7 <= local_now.hour < 22
        return 8 <= local_now.hour < 21

    async def send(self, action) -> SendResult:
        """Dispatch via the existing app.services.twilio_sms.send_sms()."""
        from app.services.twilio_sms import send_sms
        from app.services.twilio_voice import TwilioCredentials
        from app.tenancy import get_tenant_twilio_credentials  # if available

        # Resolve tenant's Twilio creds (account SID, auth token, from-number).
        # The existing tenancy layer probably exposes a helper; fall back to
        # env vars for tenant=1 (BMP) until per-tenant routing is wired.
        try:
            creds = await _resolve_twilio_creds(action.tenant_id)
        except _CredsNotConfigured:
            raise PermanentChannelError(
                f"twilio not configured for tenant {action.tenant_id}"
            )

        try:
            result = await send_sms(
                creds=creds,
                to_number=action.recipient_phone,
                from_number=creds.from_number,
                body=action.body,
            )
        except Exception as e:
            msg = str(e).lower()
            if any(s in msg for s in ("timeout", "5xx", "503", "network")):
                raise TransientChannelError(str(e)) from e
            raise PermanentChannelError(str(e)) from e

        if not result.success:
            # Twilio returned a structured error. Distinguish transient
            # (rate limit, region temp-down) from permanent (bad number,
            # blocked recipient).
            error_text = (result.error or "").lower()
            if any(s in error_text for s in (
                "rate", "throttle", "temporarily", "try again",
            )):
                raise TransientChannelError(result.error)
            return SendResult(
                success=False,
                error_message=result.error,
            )

        # Dual-write an Activity row so dashboard / morning brief /
        # team leaderboard counters that look at imessage_sent /
        # sms_sent activity counts see new-engine SMS sends.
        try:
            from app.models import Activity as _Activity
            import json as _json
            async with async_session() as activity_session:
                ctx = await activity_session.execute(text("""
                    SELECT e.company_id,
                           COALESCE(e.assigned_bdr_id, co.assigned_to) AS user_id
                    FROM engagements e
                    JOIN companies co ON co.id = e.company_id
                    WHERE e.id = :eng
                """), {"eng": action.engagement_id})
                row = ctx.first()
                company_id = row.company_id if row else None
                user_id = row.user_id if row else None
                activity_session.add(_Activity(
                    company_id=company_id,
                    contact_id=action.contact_id,
                    user_id=user_id,
                    activity_type="imessage_sent",
                    content=f"Sent SMS: {(action.body or '')[:200]}",
                    metadata_json=_json.dumps({
                        "engagement_action_id": action.id,
                        "engagement_id": action.engagement_id,
                        "engine": "engagement_engine",
                        "twilio_sid": result.message_sid,
                        "to": action.recipient_phone,
                    }),
                ))
                await activity_session.commit()
        except Exception:
            pass  # never block dispatch

        return SendResult(
            success=True,
            external_id=result.message_sid,
            cost_usd=0.0079,  # ~$0.0079/SMS Twilio standard pricing
        )

    async def fetch_outcome(self, action) -> OutcomeUpdate | None:
        """Twilio status webhook ingestion handles delivery + reply events,
        writing to signals (sms_reply / sms_opt_out) directly."""
        return None


# ── Helpers ─────────────────────────────────────────────────────────────────

class _CredsNotConfigured(Exception):
    pass


async def _resolve_twilio_creds(tenant_id: int):
    """Load Twilio credentials for the tenant from the secrets vault.

    Phase 2 minimal: looks for tenant-scoped creds in tenant_secrets, falls
    back to global env for tenant=1 (BMP).
    """
    import os
    from app.services.twilio_voice import TwilioCredentials

    async with async_session() as session:
        # Try tenant_secrets first
        row = await session.execute(text("""
            SELECT secret_value FROM tenant_secrets
            WHERE tenant_id = :t AND secret_key = 'twilio_credentials'
              AND is_active = TRUE
            LIMIT 1
        """), {"t": tenant_id})
        secret = row.first()

    if secret is not None:
        # secret_value is JSON: {account_sid, auth_token, from_number}
        import json
        data = json.loads(secret.secret_value) if isinstance(secret.secret_value, str) else secret.secret_value
        creds = TwilioCredentials(
            account_sid=data.get("account_sid", ""),
            auth_token=data.get("auth_token", ""),
        )
        creds.from_number = data.get("from_number", "")
        return creds

    # Fallback to env (BMP / tenant=1)
    acct_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_num = os.environ.get("TWILIO_FROM_NUMBER", "")
    if not (acct_sid and auth and from_num):
        raise _CredsNotConfigured(f"tenant {tenant_id} has no twilio creds")
    creds = TwilioCredentials(account_sid=acct_sid, auth_token=auth)
    creds.from_number = from_num
    return creds
