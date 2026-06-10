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

        # Build a reply-to that routes prospect replies back to THIS action.
        # Format: r-a{action_id}_{16-hex}@{reply_domain}. The "a" prefix +
        # underscore separator lets the inbound webhook distinguish new-
        # engine tokens from legacy generated_emails.reply_token (which is
        # pure hex with no underscore). The underscore is in the regex
        # allowlist ([A-Za-z0-9_-]) so the token passes the format check.
        from app.services.email_sender import reply_to_for_token
        import secrets as _secrets
        new_engine_token = f"a{action.id}_{_secrets.token_hex(8)}"
        new_reply_to = reply_to_for_token(new_engine_token)

        # POST-CUTOVER: wrap links for click tracking. The legacy path
        # ran wrap_html_links on every send; EmailChannel skipped this,
        # so every click in an engine email went directly to the dest
        # URL → ZERO TrackingLink rows, zero email_clicked Activity,
        # zero lead-score click bumps, auto-qualify-on-click never trips.
        # Resolve company_id here so the TrackingLink row attributes
        # correctly (engagement.id is meaningless to the /t/{token} handler).
        wrapped_body = action.body
        co_id_for_tracking = None
        try:
            from app.services.tracking import wrap_html_links
            async with async_session() as track_session:
                co_row = await track_session.execute(text("""
                    SELECT company_id FROM engagements WHERE id = :e
                """), {"e": action.engagement_id})
                co = co_row.first()
                co_id_for_tracking = int(co.company_id) if co else None
                wrapped_body = await wrap_html_links(
                    track_session,
                    action.body,
                    contact_id=action.contact_id,
                    company_id=co_id_for_tracking,
                    email_id=action.id,
                    label="engine_body_link",
                )
        except Exception:
            # Click-tracking is observability; never block a send if the
            # wrap fails (regex blowup on malformed HTML, etc.).
            wrapped_body = action.body

        try:
            result = await send_email(
                to_email=action.recipient_email,
                subject=action.subject,
                body=wrapped_body,
                from_name=from_name,
                from_firstname=from_firstname,
                reply_to_email=new_reply_to,
                company_id=co_id_for_tracking or action.engagement_id,
                contact_id=action.contact_id,
                email_id=action.id,  # for legacy audit-log keying
                step_type="email",
                # New: distinct tag so Resend webhooks route opens/clicks/
                # bounces to the new-engine signals table rather than to a
                # random generated_emails row that happens to share the id.
                engagement_action_id=action.id,
            )
        except Exception as e:
            # Distinguish transient (5xx, network) from permanent (4xx, hard
            # config) so the dispatcher knows whether to reschedule.
            msg = str(e).lower()
            if any(s in msg for s in ("timeout", "5xx", "503", "502", "504",
                                       "connection")):
                raise TransientChannelError(str(e)) from e
            raise PermanentChannelError(str(e)) from e

        # send_email reports API-level failures as a result dict, NOT an
        # exception. Without this check, a Resend rejection (429 rate
        # limit, 4xx validation, 5xx outage) fell through to the success
        # path below — the action was marked status='sent' with a NULL
        # external_id and the prospect never received anything. Observed
        # live on prod (2026-06-10 16:06 tick: HTTP 429 → "sent: 1").
        # Transient failures raise TransientChannelError so the dispatcher
        # reschedules; permanent ones raise PermanentChannelError → failed.
        if not result.get("success"):
            err = (f"resend rejected: HTTP {result.get('status_code', '?')} "
                   f"{str(result.get('error', ''))[:300]}")
            if result.get("retryable"):
                raise TransientChannelError(err)
            raise PermanentChannelError(err)

        # send_email returns a dict with 'resend_id' (NOT 'resend_message_id'
        # as I wrote earlier — caught during prod cutover when external_id
        # came back NULL for all 15 actual Resend sends).
        resend_id = result.get("resend_id")

        # Dual-write an Activity row so every dashboard counting
        # email_sent activity (team dashboard KPIs, BDR leaderboard,
        # morning brief, sent-this-week widget) sees the new-engine
        # sends. WITHOUT this dual-write, the dashboards undercount
        # every new-engine email by 100% — the prod symptom that
        # showed 0 emails for hours of real autopilot enrollments.
        # user_id attribution comes from the engagement's assigned
        # BDR (or the company's assigned_to), falling back to NULL
        # for system-driven actions with no rep.
        try:
            from app.models import Activity as _Activity
            import json as _json
            async with async_session() as activity_session:
                ctx = await activity_session.execute(text("""
                    SELECT e.company_id,
                           COALESCE(e.assigned_bdr_id, co.assigned_to) AS user_id,
                           co.status AS company_status
                    FROM engagements e
                    JOIN companies co ON co.id = e.company_id
                    WHERE e.id = :eng
                """), {"eng": action.engagement_id})
                row = ctx.first()
                company_id = row.company_id if row else None
                user_id = row.user_id if row else None
                company_status = row.company_status if row else None
                activity_session.add(_Activity(
                    company_id=company_id,
                    contact_id=action.contact_id,
                    user_id=user_id,
                    activity_type="email_sent",
                    content=f"Sent: {(action.subject or '(no subject)')[:200]}",
                    metadata_json=_json.dumps({
                        "engagement_action_id": action.id,
                        "engagement_id": action.engagement_id,
                        "engine": "engagement_engine",
                        "resend_id": resend_id,
                        "to": action.recipient_email,
                        "subject": action.subject,
                    }),
                ))

                # POST-CUTOVER status transition: when THIS send was the
                # LAST scheduled action on the engagement (i.e., after
                # this commit the engagement has zero remaining scheduled
                # work), flip company.status='sequencing' → 'contacted'.
                # 'contacted' is the mid-funnel bucket meaning "we
                # finished cold outreach but the prospect didn't react".
                # Pre-fix, engine engagements would silently complete
                # and leave the company stuck in 'sequencing' forever —
                # the 'Contacted' filter pill stayed empty for engine
                # contacts (Finding 8).
                if company_status == "sequencing" and company_id is not None:
                    remaining = await activity_session.execute(text("""
                        SELECT COUNT(*) FROM actions
                        WHERE engagement_id = :eng
                          AND status = 'scheduled'
                          AND id != :this_id
                    """), {"eng": action.engagement_id, "this_id": action.id})
                    if int(remaining.scalar() or 0) == 0:
                        await activity_session.execute(text("""
                            UPDATE companies
                            SET status = 'contacted'
                            WHERE id = :co AND status = 'sequencing'
                        """), {"co": company_id})

                # POST-CUTOVER: also set the denormalized
                # company.email_sent flag for parity with legacy. The
                # column is currently only consumed by external/future
                # integrations (no current SQL read path), but setting
                # it consistently means engine-only companies don't
                # appear as "never emailed" if anything starts using it.
                if company_id is not None:
                    await activity_session.execute(text("""
                        UPDATE companies SET email_generated = TRUE
                        WHERE id = :co AND email_generated = FALSE
                    """), {"co": company_id})

                await activity_session.commit()
        except Exception:
            # Activity logging must NEVER block dispatch — the engine
            # already recorded the canonical send in `actions`.
            pass

        return SendResult(
            success=True,
            external_id=resend_id,
            cost_usd=0.0004,  # ~$0.40 per 1000 emails Resend pricing
        )

    async def fetch_outcome(self, action) -> OutcomeUpdate | None:
        """Resend webhook ingestion will write opens/clicks/bounces directly
        to the signals + email_suppressions tables. Polling not needed."""
        return None
