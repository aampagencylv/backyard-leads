"""ManualChannel — surfaces non-automated BDR actions as CRM tasks.

Used for LinkedIn DMs (until Phase 8 ships a real LinkedInChannel) and
any other channel where the BDR does the actual outreach manually.

Same architecture as CallTaskChannel: writes a row to the legacy `tasks`
table with `engagement_action_id` for round-trip linkage. BDR sees the
task in the existing CRM, completes it normally, the linked action
gets marked completed via PATCH /api/crm/tasks/{id}/complete.
"""
from __future__ import annotations
import logging
from datetime import datetime

from sqlalchemy import text

from app.database import async_session
from app.engagement_engine.interfaces import (
    ActionDispatcher,
    GuardResult,
    SendResult,
    OutcomeUpdate,
    PermanentChannelError,
)

log = logging.getLogger("engagement_engine.channels.manual")


class ManualChannel:
    """Writes manual BDR actions as legacy CRM tasks."""

    channel_code: str = "manual"

    async def pre_dispatch_guards(self, action) -> GuardResult:
        # Manual actions need *some* instruction (subject or body or task);
        # if all three are empty the BDR has nothing to act on.
        has_content = any([
            (action.task_description or "").strip(),
            (action.subject or "").strip(),
            (action.body or "").strip(),
        ])
        if not has_content:
            return GuardResult(blocked=True, reason="empty_manual_content")
        return GuardResult(blocked=False)

    async def is_in_send_window(self, local_now: datetime, tcpa_b2b_override: bool) -> bool:
        # Same as call_task: queueing for BDR is fine any time; the actual
        # outreach happens when the BDR processes the task.
        return True

    async def send(self, action) -> SendResult:
        """Create a legacy CRM Task row linked to this action."""
        async with async_session() as session:
            # Idempotency
            existing = await session.execute(text("""
                SELECT id FROM tasks WHERE engagement_action_id = :aid
            """), {"aid": action.id})
            existing_row = existing.first()
            if existing_row is not None:
                return SendResult(
                    success=True,
                    external_id=f"task:{existing_row.id}",
                    cost_usd=0.0,
                )

            ctx = await session.execute(text("""
                SELECT
                    e.company_id,
                    e.contact_id,
                    COALESCE(e.assigned_bdr_id, co.assigned_to) AS user_id
                FROM engagements e
                JOIN companies co ON co.id = e.company_id
                WHERE e.id = :eng
            """), {"eng": action.engagement_id})
            c = ctx.first()
            if c is None:
                raise PermanentChannelError(
                    f"engagement {action.engagement_id} not found"
                )
            if c.user_id is None:
                raise PermanentChannelError(
                    f"no BDR assigned for engagement {action.engagement_id}"
                )

            # Build description from whatever the action carries.
            # Prefer task_description, else subject, else body excerpt.
            desc_source = (
                action.task_description
                or action.subject
                or (action.body or "")[:300]
                or "Manual outreach"
            )
            description = f"[Manual] {desc_source}"[:500]

            result = await session.execute(text("""
                INSERT INTO tasks (
                    tenant_id, company_id, contact_id, user_id,
                    description, due_date, engagement_action_id, completed
                )
                VALUES (
                    :t, :co, :c, :u,
                    :desc, :due, :aid, FALSE
                )
                RETURNING id
            """), {
                "t": action.tenant_id,
                "co": c.company_id,
                "c": c.contact_id,
                "u": c.user_id,
                "desc": description,
                "due": action.scheduled_at,
                "aid": action.id,
            })
            task_id = result.first().id
            await session.commit()

        log.info(
            "manual action %s → created CRM task %s for user %s",
            action.id, task_id, c.user_id,
        )
        return SendResult(
            success=True,
            external_id=f"task:{task_id}",
            cost_usd=0.0,
        )

    async def fetch_outcome(self, action) -> OutcomeUpdate | None:
        return None
