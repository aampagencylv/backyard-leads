"""Pre-dispatch kill-switch and stale-action checks.

Centralizes the gates from Rule #9 (kill switches) and the stale-action
detection (Section 7 of the defensive architecture). Called from the
dispatcher BEFORE any channel-specific guard. Order matters — checks are
arranged cheapest-first so failed dispatches get short-circuited early.

The hierarchy of "who can halt outreach":
    1. engagement.status = 'terminal'             — engine-level (auto)
    2. contact.do_not_contact = TRUE              — contact-level (CRM action)
    3. company.do_not_contact = TRUE              — company-level (CRM action)
    4. contact.outreach_owner != 'engagement_engine'
                                                  — routing (cutover phase)
    5. channel_types.is_paused = TRUE             — channel-level (incident)

Stale-action checks:
    6. engagement.last_reply_at > action.created_at
       → prospect replied between schedule and dispatch; the queued action
         is stale and should be dropped.
    7. action.stale_after < NOW()                 — too old to send
    8. action.superseded_by_action_id IS NOT NULL — replaced by a newer action

Recipient drift check (the v3 fix for B3):
    9. action.recipient_email != contact.email at dispatch time
       (B3 closed the gap where contact email changes AFTER an action was
        scheduled but BEFORE dispatch.)

Channel-specific guards (warmup cap, TCPA quiet hours, suppression) are
NOT here — those live in each ActionDispatcher's pre_dispatch_guards.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass
class DispatchEligibility:
    """Outcome of running all pre-dispatch gates on an action."""
    eligible: bool
    block_reason: str | None = None  # populated when eligible=False


@dataclass
class _ActionContext:
    """Joined view of the action + its engagement + contact + company for
    cheap kill-switch checks. Fetched once per action; avoids N+1 queries."""
    action_id: int
    engagement_id: int
    engagement_status: str
    last_reply_at: datetime | None
    contact_id: int
    contact_email: str | None
    contact_phone: str | None
    contact_linkedin_url: str | None
    contact_do_not_contact: bool
    contact_outreach_owner: str
    company_id: int
    company_do_not_contact: bool
    channel_id: int
    channel_code: str
    channel_is_paused: bool
    action_created_at: datetime
    action_stale_after: datetime
    action_superseded_by: int | None
    action_recipient_email: str | None
    action_recipient_phone: str | None
    action_recipient_linkedin_url: str | None


async def check_dispatch_eligibility(
    conn: AsyncConnection, *, action_id: int,
) -> DispatchEligibility:
    """Run all 9 pre-dispatch gates for a single action.

    Returns DispatchEligibility(eligible=True) when the action passes all
    gates. Otherwise returns the FIRST failing block_reason (caller marks
    action.status='skipped' or 'blocked' and records the reason).

    Single SQL JOIN to load everything we need — N+1 prevention.
    """
    row = await conn.execute(text("""
        SELECT
            a.id              AS action_id,
            a.engagement_id   AS engagement_id,
            e.status          AS engagement_status,
            e.last_reply_at   AS last_reply_at,
            c.id              AS contact_id,
            c.email           AS contact_email,
            c.phone           AS contact_phone,
            c.linkedin_url    AS contact_linkedin_url,
            c.do_not_contact  AS contact_do_not_contact,
            c.outreach_owner  AS contact_outreach_owner,
            co.id             AS company_id,
            co.do_not_contact AS company_do_not_contact,
            ct.id             AS channel_id,
            ct.code           AS channel_code,
            ct.is_paused      AS channel_is_paused,
            a.created_at      AS action_created_at,
            a.stale_after     AS action_stale_after,
            a.superseded_by_action_id AS action_superseded_by,
            a.recipient_email AS action_recipient_email,
            a.recipient_phone AS action_recipient_phone,
            a.recipient_linkedin_url AS action_recipient_linkedin_url
        FROM actions a
        JOIN engagements e  ON e.id = a.engagement_id
        JOIN contacts c     ON c.id = a.contact_id
        JOIN companies co   ON co.id = e.company_id
        JOIN channel_types ct ON ct.id = a.channel_id
        WHERE a.id = :action_id
    """), {"action_id": action_id})
    r = row.first()
    if r is None:
        return DispatchEligibility(eligible=False, block_reason="action_not_found")

    ctx = _ActionContext(
        action_id=r.action_id,
        engagement_id=r.engagement_id,
        engagement_status=r.engagement_status,
        last_reply_at=r.last_reply_at,
        contact_id=r.contact_id,
        contact_email=r.contact_email,
        contact_phone=r.contact_phone,
        contact_linkedin_url=r.contact_linkedin_url,
        contact_do_not_contact=r.contact_do_not_contact,
        contact_outreach_owner=r.contact_outreach_owner,
        company_id=r.company_id,
        company_do_not_contact=r.company_do_not_contact,
        channel_id=r.channel_id,
        channel_code=r.channel_code,
        channel_is_paused=r.channel_is_paused,
        action_created_at=r.action_created_at,
        action_stale_after=r.action_stale_after,
        action_superseded_by=r.action_superseded_by,
        action_recipient_email=r.action_recipient_email,
        action_recipient_phone=r.action_recipient_phone,
        action_recipient_linkedin_url=r.action_recipient_linkedin_url,
    )
    return _check_all_gates(ctx)


def _check_all_gates(ctx: _ActionContext) -> DispatchEligibility:
    """All gates in priority order. First failure wins."""

    # ── Kill switches (Rule #9) ─────────────────────────────────────────────
    if ctx.engagement_status == "terminal":
        return DispatchEligibility(False, "engagement_terminal")
    if ctx.engagement_status == "paused":
        return DispatchEligibility(False, "engagement_paused")
    if ctx.contact_do_not_contact:
        return DispatchEligibility(False, "contact_do_not_contact")
    if ctx.company_do_not_contact:
        return DispatchEligibility(False, "company_do_not_contact")
    if ctx.contact_outreach_owner != "engagement_engine":
        # During cutover this matters most; gives a clean reason for audit.
        return DispatchEligibility(
            False, f"outreach_owner={ctx.contact_outreach_owner}",
        )
    if ctx.channel_is_paused:
        return DispatchEligibility(
            False, f"channel_paused:{ctx.channel_code}",
        )

    # ── Stale-action checks (defensive architecture §7) ─────────────────────
    if (ctx.last_reply_at is not None
            and ctx.last_reply_at > ctx.action_created_at):
        return DispatchEligibility(False, "stale_post_reply")
    if ctx.action_superseded_by is not None:
        return DispatchEligibility(False, "superseded")
    if ctx.action_stale_after < datetime.now(timezone.utc):
        return DispatchEligibility(False, "stale_too_old")

    # ── Recipient drift check (v3 fix B3) ───────────────────────────────────
    # If the action's recipient_email was scheduled to one value, but the
    # contact's email has since changed, BLOCK — the queued send would go
    # to a stale address.
    if (ctx.action_recipient_email is not None
            and ctx.action_recipient_email != ctx.contact_email):
        return DispatchEligibility(False, "recipient_drift_email")
    if (ctx.action_recipient_phone is not None
            and ctx.action_recipient_phone != ctx.contact_phone):
        return DispatchEligibility(False, "recipient_drift_phone")
    if (ctx.action_recipient_linkedin_url is not None
            and ctx.action_recipient_linkedin_url != ctx.contact_linkedin_url):
        return DispatchEligibility(False, "recipient_drift_linkedin")

    return DispatchEligibility(eligible=True)


# ── Pure-function variant for unit testing ──────────────────────────────────

def check_gates_for_context(ctx: _ActionContext) -> DispatchEligibility:
    """Public alias of the private gate-checker, for use in tests that
    construct an _ActionContext directly without hitting the DB."""
    return _check_all_gates(ctx)
