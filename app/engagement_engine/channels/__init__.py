"""Pluggable channel adapters.

Each adapter implements the `ActionDispatcher` Protocol from
app.engagement_engine.interfaces and is registered here by channel code.

The dispatcher looks up the adapter for an action via:
    adapter = get_channel(action.channel_code)
    await adapter.pre_dispatch_guards(action)
    await adapter.send(action)

Adding a new channel:
  1. Create the adapter class implementing ActionDispatcher
  2. Add to CHANNEL_REGISTRY below
  3. INSERT INTO channel_types (code, label) at DB level
  4. Workers receive LISTEN/NOTIFY refresh on the next tick
"""
from __future__ import annotations
from app.engagement_engine.interfaces import ActionDispatcher
from app.engagement_engine.channels.manual import ManualChannel
from app.engagement_engine.channels.call_task import CallTaskChannel
from app.engagement_engine.channels.email import EmailChannel
from app.engagement_engine.channels.sms import SMSChannel


CHANNEL_REGISTRY: dict[str, ActionDispatcher] = {
    "email":     EmailChannel(),
    "sms":       SMSChannel(),
    "call_task": CallTaskChannel(),
    "manual":    ManualChannel(),
    # 'linkedin' deferred to Phase 8
    # 'wait' is a no-op step — dispatcher skips it without lookup
}


def get_channel(channel_code: str) -> ActionDispatcher | None:
    """Look up the adapter for a channel code. Returns None if not
    registered — caller marks the action failed with
    skip_reason='no_adapter:{channel_code}'."""
    return CHANNEL_REGISTRY.get(channel_code)


def supported_channels() -> list[str]:
    """List of channel codes the engine can currently dispatch."""
    return sorted(CHANNEL_REGISTRY.keys())
