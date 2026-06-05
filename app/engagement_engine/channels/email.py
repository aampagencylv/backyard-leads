"""EmailChannel — outbound email via Resend.

Wraps the existing app.services.email_sender.send_email() so we don't
reimplement the Resend integration, but adds engagement-engine-specific
pre-dispatch guards:

  1. Suppression-list check — hard-bounce / complaint / unsubscribe history
     blocks any further send to the same recipient.
  2. Identity warmup cap — early-warmup senders limited to 50/day, growing
     weekly. Atomic UPDATE-WHERE pattern (B12) handles the increment safely
     across multiple dispatcher instances.
  3. Empty-subject guard (Gmail/Outlook reputation killer).
  4. Placeholder-text guard (Texas Remodel Team class incident defense).

STAGING_FORCE_RECIPIENT is handled by the underlying send_email function —
the DB record keeps the real recipient for audit, while the SMTP envelope
goes to the staging mailbox.
"""
from __future__ import annotations
import os
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


# Placeholder substrings — when the AI / template renderer dropped raw
# placeholder text into the body, we refuse to send. Same defense as the
# legacy `email_sender.py` belt-and-suspenders layer.
PLACEHOLDER_FRAGMENTS = (
    "{{",
    "[unrendered",
    "[Skipped]",
    "iMessage step",
    "Call step",
    "Linkedin step",
    "Call ",  # "Call 3", "Call 5" — bare placeholders sneaked into subject
)


class EmailChannel:
    """Outbound email via Resend, with warmup + suppression + content guards."""

    channel_code: str = "email"

    async def pre_dispatch_guards(self, action) -> GuardResult:
        # 1. Empty subject (deliverability killer)
        if not (action.subject or "").strip():
            return GuardResult(blocked=True, reason="empty_subject")

        # 2. Empty body
        if not (action.body or "").strip():
            return GuardResult(blocked=True, reason="empty_body")

        # 3. Placeholder text leak detector
        subject = action.subject or ""
        body = action.body or ""
        for frag in PLACEHOLDER_FRAGMENTS:
            if frag in subject:
                return GuardResult(blocked=True,
                                   reason=f"placeholder_in_subject:{frag.strip()}")
            if frag in body:
                return GuardResult(blocked=True,
                                   reason=f"placeholder_in_body:{frag.strip()}")

        # 4. Suppression-list check
        async with async_session() as session:
            sup = await session.execute(text("""
                SELECT 1 FROM email_suppressions
                WHERE tenant_id = :t
                  AND recipient_email = :e
                  AND is_currently_active = TRUE
                LIMIT 1
            """), {"t": action.tenant_id, "e": action.recipient_email})
            if sup.first() is not None:
                return GuardResult(blocked=True, reason="suppression_listed")

            # 5. Warmup-cap atomic check + increment
            # Find the identity row for this tenant's first active sender.
            # In Phase 2 we don't yet have per-action identity selection;
            # the tenant has one active identity. Phase 8+ may add routing.
            identity_row = await session.execute(text("""
                SELECT id, daily_send_cap, sent_today, sent_today_date,
                       reset_timezone, warmup_stage
                FROM email_identities
                WHERE tenant_id = :t AND is_active = TRUE
                ORDER BY id
                LIMIT 1
            """), {"t": action.tenant_id})
            ident = identity_row.first()

            if ident is not None:
                # Atomic UPDATE-WHERE with TZ-aware day rollover.
                # Either resets sent_today to 1 (new day) or increments by 1
                # if still under cap. Zero rows returned → cap hit.
                bump = await session.execute(text("""
                    UPDATE email_identities
                    SET sent_today = CASE
                            WHEN sent_today_date < (NOW() AT TIME ZONE reset_timezone)::date THEN 1
                            ELSE sent_today + 1
                        END,
                        sent_today_date = (NOW() AT TIME ZONE reset_timezone)::date
                    WHERE id = :id
                      AND CASE
                            WHEN sent_today_date < (NOW() AT TIME ZONE reset_timezone)::date THEN 1
                            ELSE sent_today + 1
                          END <= daily_send_cap
                      AND warmup_stage != 'paused'
                    RETURNING sent_today
                """), {"id": ident.id})
                if bump.first() is None:
                    if ident.warmup_stage == "paused":
                        return GuardResult(blocked=True,
                                           reason="email_identity_paused")
                    return GuardResult(blocked=True,
                                       reason=f"warmup_cap_hit_{ident.daily_send_cap}")
                await session.commit()

        return GuardResult(blocked=False)

    async def is_in_send_window(self, local_now: datetime, tcpa_b2b_override: bool) -> bool:
        """Email: 7am–10pm local for cold outreach. Phase 2 doesn't yet know
        the engagement's phase here, so we apply the wider window. Reschedule
        rather than reject — sleeping prospects skew opens lower."""
        return 7 <= local_now.hour < 22

    async def send(self, action) -> SendResult:
        """Dispatch via the existing app.services.email_sender.send_email().

        The legacy sender already handles:
          - STAGING_FORCE_RECIPIENT rewrite
          - timeouts (connect=8s, read=30s, write=15s)
          - Resend API + retry semantics
          - audit log row
        """
        # Import here to avoid circular import at module load (email_sender
        # may import from models which we're not ready for).
        from app.services.email_sender import send_email, get_sender_info

        # Resolve sender identity. Resolution order:
        #   1. tenant's email_identities row (the future-correct path; BYO
        #      identity via the CRM UI)
        #   2. fallback: look up the engagement's assigned BDR and use
        #      get_sender_info(first_name, full_name) — exactly the same
        #      derivation the legacy engine uses, so cutover preserves
        #      sender continuity without any data seeding
        async with async_session() as session:
            ident_row = await session.execute(text("""
                SELECT sender_email, sender_name
                FROM email_identities
                WHERE tenant_id = :t AND is_active = TRUE
                ORDER BY id LIMIT 1
            """), {"t": action.tenant_id})
            ident = ident_row.first()

            if ident is not None:
                from_name = ident.sender_name or "Backyard Marketing Pros"
                from_firstname = (ident.sender_name or "BMP").split(" ")[0]
                reply_to_email = ident.sender_email
            else:
                # Legacy fallback: derive from the engagement's assigned BDR.
                # Look up the BDR via engagement.assigned_bdr_id; if absent,
                # use the company's assigned_to; if absent, fail.
                # NOTE: users.full_name is a Python @property on the ORM,
                # not a DB column — assemble from first_name + last_name.
                bdr_row = await session.execute(text("""
                    SELECT u.first_name, u.last_name
                    FROM engagements e
                    LEFT JOIN companies co ON co.id = e.company_id
                    LEFT JOIN users u ON u.id = COALESCE(
                        e.assigned_bdr_id, co.assigned_to
                    )
                    WHERE e.id = :eng
                """), {"eng": action.engagement_id})
                bdr = bdr_row.first()
                if bdr is None or not (bdr.first_name or bdr.last_name):
                    raise PermanentChannelError(
                        f"no email_identity AND no assignable BDR for "
                        f"engagement {action.engagement_id}"
                    )
                full_name = f"{bdr.first_name or ''} {bdr.last_name or ''}".strip()
                derived = get_sender_info(bdr.first_name, full_name)
                from_name = derived["from_name"]
                from_firstname = derived["from_firstname"]
                reply_to_email = derived["reply_to"]

        try:
            result = await send_email(
                to_email=action.recipient_email,
                subject=action.subject,
                body=action.body,
                from_name=from_name,
                from_firstname=from_firstname,
                reply_to_email=reply_to_email,
                company_id=action.engagement_id,  # legacy field; eng_id works
                contact_id=action.contact_id,
                email_id=action.id,  # for legacy audit-log keying
                step_type="email",
            )
        except Exception as e:
            # Distinguish transient (5xx, network) from permanent (4xx, hard
            # config) so the dispatcher knows whether to reschedule.
            msg = str(e).lower()
            if any(s in msg for s in ("timeout", "5xx", "503", "502", "504",
                                       "connection")):
                raise TransientChannelError(str(e)) from e
            raise PermanentChannelError(str(e)) from e

        # send_email returns a dict with 'resend_id' (NOT 'resend_message_id'
        # as I wrote earlier — caught during prod cutover when external_id
        # came back NULL for all 15 actual Resend sends).
        return SendResult(
            success=True,
            external_id=result.get("resend_id"),
            cost_usd=0.0004,  # ~$0.40 per 1000 emails Resend pricing
        )

    async def fetch_outcome(self, action) -> OutcomeUpdate | None:
        """Resend webhook ingestion will write opens/clicks/bounces directly
        to the signals + email_suppressions tables. Polling not needed."""
        return None
