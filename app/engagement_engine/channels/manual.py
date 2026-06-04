"""ManualChannel — for actions that a BDR handles manually.

The dispatcher doesn't actually 'send' anything for this channel — it just
records the action in the actions table and surfaces it in the BDR's queue.
The BDR marks it completed via the CRM UI.

This is the simplest adapter; serves as a reference implementation of the
ActionDispatcher contract for the others.
"""
from __future__ import annotations
from datetime import datetime

from app.engagement_engine.interfaces import (
    ActionDispatcher,
    GuardResult,
    SendResult,
    OutcomeUpdate,
)


class ManualChannel:
    """No-op dispatcher. BDR handles the action in the CRM."""

    channel_code: str = "manual"

    async def pre_dispatch_guards(self, action) -> GuardResult:
        # Manual sends always pass — BDR oversight is the safety mechanism.
        return GuardResult(blocked=False)

    async def is_in_send_window(self, local_now: datetime, tcpa_b2b_override: bool) -> bool:
        # Manual actions can be created any time; the BDR decides when to act.
        return True

    async def send(self, action) -> SendResult:
        # No actual dispatch. The dispatcher will mark this action 'sent'
        # (=surfaced to BDR queue); BDR completes it manually via CRM.
        return SendResult(
            success=True,
            external_id=None,
            error_message=None,
            cost_usd=0.0,
        )

    async def fetch_outcome(self, action) -> OutcomeUpdate | None:
        # Outcome is set by the BDR via CRM, not polled.
        return None
