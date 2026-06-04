"""CallTaskChannel — creates a BDR phone-call task in the CRM.

Similar to ManualChannel in that the dispatcher doesn't make the call
itself; it surfaces a task into the BDR's queue. Difference: the action
carries explicit task_description telling the BDR what to say (often
AI-generated from a high-relevance signal).

The BDR completes the task in the CRM, which writes a 'call_outcome'
signal back into the engagement.
"""
from __future__ import annotations
from datetime import datetime

from app.engagement_engine.interfaces import (
    ActionDispatcher,
    GuardResult,
    SendResult,
    OutcomeUpdate,
)


class CallTaskChannel:
    """BDR phone-call task dispatcher."""

    channel_code: str = "call_task"

    async def pre_dispatch_guards(self, action) -> GuardResult:
        # Require non-empty task description so BDR isn't staring at nothing
        if not (action.task_description or "").strip():
            return GuardResult(
                blocked=True, reason="empty_task_description",
            )
        # Require an assigned BDR — handled at engagement level via
        # engagements.assigned_bdr_id. If unassigned, task lands in the
        # tenant's unassigned queue (dispatcher logs but doesn't block).
        return GuardResult(blocked=False)

    async def is_in_send_window(self, local_now: datetime, tcpa_b2b_override: bool) -> bool:
        # Surfacing a task to a BDR's queue is always OK — the BDR decides
        # when to actually call (and call-task channels are subject to their
        # own TCPA windows when the call happens, not here).
        return True

    async def send(self, action) -> SendResult:
        # No outbound transport — the task already exists as an action row
        # with task_description populated. CRM surfaces it from
        # actions WHERE status='sent' AND channel='call_task'.
        return SendResult(
            success=True,
            external_id=None,
            cost_usd=0.0,
        )

    async def fetch_outcome(self, action) -> OutcomeUpdate | None:
        # Outcome is recorded by the BDR when they complete the call.
        # Polled by the CRM-event ingestion path, not here.
        return None
