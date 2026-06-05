"""CallTaskChannel — surfaces a BDR phone-call action as a CRM task.

The new engine is the sole source of truth for outreach. When the
dispatcher fires a call_task action, this adapter writes a row to the
existing legacy `tasks` table with `engagement_action_id` pointing back
to the action. The BDR sees the task in their existing CRM task view
(no UI changes needed). When the BDR completes the task in the CRM,
the matching action gets marked completed via the
PATCH /api/crm/tasks/{id}/complete endpoint.

Idempotency: the UNIQUE (engagement_action_id WHERE NOT NULL) index on
tasks prevents duplicate creation if the dispatcher re-tries the action.
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

log = logging.getLogger("engagement_engine.channels.call_task")


class CallTaskChannel:
    """BDR phone-call task dispatcher: writes to legacy tasks table."""

    channel_code: str = "call_task"

    async def pre_dispatch_guards(self, action) -> GuardResult:
        # Require non-empty task description so BDR isn't staring at nothing
        if not (action.task_description or "").strip():
            return GuardResult(blocked=True, reason="empty_task_description")
        return GuardResult(blocked=False)

    async def is_in_send_window(self, local_now: datetime, tcpa_b2b_override: bool) -> bool:
        # Tasks land in BDR queue any time — BDR decides when to actually
        # make the call. Quiet hours apply when the call happens, not here.
        return True

    async def send(self, action) -> SendResult:
        """Create a legacy CRM Task row linked to this action.

        Returns:
            SendResult.external_id = "task:{id}" for traceability.

        Raises:
            PermanentChannelError if no BDR can be resolved for the engagement.
        """
        async with async_session() as session:
            # Idempotency check first: did we already create a task?
            existing = await session.execute(text("""
                SELECT id FROM tasks WHERE engagement_action_id = :aid
            """), {"aid": action.id})
            existing_row = existing.first()
            if existing_row is not None:
                # Already created — return success with the existing task id.
                return SendResult(
                    success=True,
                    external_id=f"task:{existing_row.id}",
                    cost_usd=0.0,
                )

            # Resolve company + BDR via the engagement
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
                    f"no BDR assigned for engagement {action.engagement_id} "
                    f"(company.assigned_to and engagement.assigned_bdr_id both NULL)"
                )

            description = (
                action.task_description
                or action.subject
                or "Call this prospect"
            )[:500]

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
            "call_task action %s → created CRM task %s for user %s",
            action.id, task_id, c.user_id,
        )
        return SendResult(
            success=True,
            external_id=f"task:{task_id}",
            cost_usd=0.0,
        )

    async def fetch_outcome(self, action) -> OutcomeUpdate | None:
        # Outcome is set by the BDR via the existing PATCH /api/crm/tasks/
        # endpoint, which has been updated to also mark the linked action
        # as completed. No polling needed.
        return None
