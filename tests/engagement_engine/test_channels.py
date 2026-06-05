"""Tests for channel adapters — the pure-function parts.

DB-dependent parts (suppression check, warmup increment, Twilio send) are
exercised on staging. Here we cover:
  - Channel registry membership
  - is_in_send_window per-channel quiet-hour rules
  - Pre-dispatch guard logic that doesn't touch the DB
"""
from datetime import datetime, timezone
import pytest

from app.engagement_engine.channels import (
    CHANNEL_REGISTRY, get_channel, supported_channels,
)
from app.engagement_engine.channels.manual import ManualChannel
from app.engagement_engine.channels.call_task import CallTaskChannel
from app.engagement_engine.channels.email import EmailChannel
from app.engagement_engine.channels.sms import SMSChannel


# ── Registry ────────────────────────────────────────────────────────────────

def test_registry_has_four_channels():
    assert set(supported_channels()) == {"email", "sms", "call_task", "manual"}


def test_get_channel_returns_correct_adapter():
    assert isinstance(get_channel("email"), EmailChannel)
    assert isinstance(get_channel("sms"), SMSChannel)
    assert isinstance(get_channel("call_task"), CallTaskChannel)
    assert isinstance(get_channel("manual"), ManualChannel)


def test_unknown_channel_returns_none():
    assert get_channel("not_a_channel") is None
    assert get_channel("whatsapp") is None  # future: add when ready


def test_each_adapter_advertises_correct_code():
    for code, adapter in CHANNEL_REGISTRY.items():
        assert adapter.channel_code == code


# ── Send-window rules ──────────────────────────────────────────────────────

def _local(hour: int) -> datetime:
    """Synthetic local-now at the given hour."""
    return datetime(2026, 6, 4, hour, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_email_send_window_7am_to_10pm():
    ch = EmailChannel()
    # In window
    assert await ch.is_in_send_window(_local(7), tcpa_b2b_override=False)
    assert await ch.is_in_send_window(_local(14), tcpa_b2b_override=False)
    assert await ch.is_in_send_window(_local(21), tcpa_b2b_override=False)
    # Out of window
    assert not await ch.is_in_send_window(_local(6), tcpa_b2b_override=False)
    assert not await ch.is_in_send_window(_local(22), tcpa_b2b_override=False)
    assert not await ch.is_in_send_window(_local(3), tcpa_b2b_override=False)


@pytest.mark.asyncio
async def test_sms_consumer_window_8am_to_9pm():
    """TCPA: SMS to consumers between 9pm-8am local is illegal."""
    ch = SMSChannel()
    # In window
    assert await ch.is_in_send_window(_local(8), tcpa_b2b_override=False)
    assert await ch.is_in_send_window(_local(20), tcpa_b2b_override=False)
    # Out of window
    assert not await ch.is_in_send_window(_local(7), tcpa_b2b_override=False)
    assert not await ch.is_in_send_window(_local(21), tcpa_b2b_override=False)
    assert not await ch.is_in_send_window(_local(22), tcpa_b2b_override=False)
    assert not await ch.is_in_send_window(_local(2), tcpa_b2b_override=False)


@pytest.mark.asyncio
async def test_sms_b2b_override_relaxes_to_7am_10pm():
    ch = SMSChannel()
    # Now allowed under override
    assert await ch.is_in_send_window(_local(7), tcpa_b2b_override=True)
    assert await ch.is_in_send_window(_local(21), tcpa_b2b_override=True)
    # Still blocked outside even with override (no overnight)
    assert not await ch.is_in_send_window(_local(6), tcpa_b2b_override=True)
    assert not await ch.is_in_send_window(_local(22), tcpa_b2b_override=True)
    assert not await ch.is_in_send_window(_local(2), tcpa_b2b_override=True)


@pytest.mark.asyncio
async def test_manual_channel_always_open():
    ch = ManualChannel()
    for h in range(24):
        assert await ch.is_in_send_window(_local(h), tcpa_b2b_override=False)


@pytest.mark.asyncio
async def test_call_task_always_open():
    """BDR phone-call tasks are queued any time; the BDR makes the actual
    call during legitimate hours."""
    ch = CallTaskChannel()
    for h in range(24):
        assert await ch.is_in_send_window(_local(h), tcpa_b2b_override=False)


# ── Manual + CallTask pre-dispatch guards (no DB) ──────────────────────────

class _Action:
    """Minimal mock — defaults every attribute to None so the real
    channel code (which now reads multiple action fields post-cutover)
    doesn't AttributeError on un-passed kwargs. Tests that care about
    a specific value pass it explicitly via kwargs."""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def __getattr__(self, name):
        # Called only when the attribute is genuinely missing — pass-by-
        # kwargs values land on __dict__ and short-circuit this.
        return None


@pytest.mark.asyncio
async def test_manual_guards_always_pass():
    """ManualChannel.pre_dispatch_guards now requires SOME instruction
    (task_description, subject, or body). With all three None the guard
    blocks. Pass a task_description so the guard sees real content."""
    ch = ManualChannel()
    result = await ch.pre_dispatch_guards(_Action(task_description="follow up via Slack"))
    assert result.blocked is False


@pytest.mark.asyncio
async def test_call_task_requires_task_description():
    ch = CallTaskChannel()
    result = await ch.pre_dispatch_guards(_Action(task_description=""))
    assert result.blocked is True
    assert result.reason == "empty_task_description"

    result = await ch.pre_dispatch_guards(_Action(task_description="   "))
    assert result.blocked is True  # whitespace-only also rejected


@pytest.mark.asyncio
async def test_call_task_with_description_passes():
    ch = CallTaskChannel()
    result = await ch.pre_dispatch_guards(
        _Action(task_description="Call Tim — 3rd location opened, congratulate")
    )
    assert result.blocked is False


# Post-cutover the Manual + CallTask channel adapters' send() methods
# create real CRM Task rows via DB I/O — they're integration-shaped, not
# unit-testable from a mock. The end-to-end coverage moved to
# scripts/validate_lifecycle.py which exercises the same path against a
# throwaway contact on prod. Keep these stubs marked skipped so the
# intent is documented but CI doesn't choke on them.
@pytest.mark.skip(reason="ManualChannel.send creates Task rows via DB — covered by scripts/validate_lifecycle.py")
@pytest.mark.asyncio
async def test_manual_send_returns_success_no_external_id():
    ch = ManualChannel()
    result = await ch.send(_Action())
    assert result.success is True
    assert result.external_id is None


@pytest.mark.skip(reason="CallTaskChannel.send creates Task rows via DB — covered by scripts/validate_lifecycle.py")
@pytest.mark.asyncio
async def test_call_task_send_returns_success_no_external_id():
    ch = CallTaskChannel()
    result = await ch.send(_Action(task_description="x"))
    assert result.success is True


# ── Manual + CallTask outcome fetch ────────────────────────────────────────

@pytest.mark.asyncio
async def test_manual_outcome_is_none():
    ch = ManualChannel()
    assert await ch.fetch_outcome(_Action()) is None


@pytest.mark.asyncio
async def test_call_task_outcome_is_none():
    ch = CallTaskChannel()
    assert await ch.fetch_outcome(_Action()) is None
