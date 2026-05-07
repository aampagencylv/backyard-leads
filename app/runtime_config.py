"""
Runtime configuration helpers.

Per-row config (currently single-row) lives in the runtime_config table and
overrides env-var defaults. This lets the team rotate API keys (e.g. Netrows)
from the Settings UI without SSHing into the server.

Read path is: DB row → env-var fallback. Write path goes through the
Settings UI (PATCH /api/runtime-config).
"""
from __future__ import annotations
from typing import Optional
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import RuntimeConfig
from app.config import settings
from app.services.twilio_voice import TwilioCredentials


async def _get_or_create(db: AsyncSession) -> RuntimeConfig:
    rc = (await db.execute(select(RuntimeConfig).where(RuntimeConfig.id == 1))).scalar_one_or_none()
    if rc is None:
        rc = RuntimeConfig(id=1)
        db.add(rc)
        await db.commit()
        await db.refresh(rc)
    return rc


async def get_netrows_api_key(db: AsyncSession) -> str:
    rc = await _get_or_create(db)
    return (rc.netrows_api_key or "").strip() or settings.netrows_api_key or ""


async def set_netrows_api_key(db: AsyncSession, value: str) -> RuntimeConfig:
    rc = await _get_or_create(db)
    rc.netrows_api_key = value.strip() or None
    rc.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(rc)
    return rc


async def get_twilio_credentials(db: AsyncSession) -> TwilioCredentials:
    """Pull Twilio creds from runtime_config; field-level env fallback."""
    rc = await _get_or_create(db)
    return TwilioCredentials(
        account_sid=(rc.twilio_account_sid or "").strip() or "",
        auth_token=(rc.twilio_auth_token or "").strip() or "",
        api_key_sid=(rc.twilio_api_key_sid or "").strip() or None,
        api_key_secret=(rc.twilio_api_key_secret or "").strip() or None,
        twiml_app_sid=(rc.twilio_twiml_app_sid or "").strip() or None,
    )


async def set_twilio_credentials(
    db: AsyncSession,
    *,
    account_sid: str | None = None,
    auth_token: str | None = None,
    api_key_sid: str | None = None,
    api_key_secret: str | None = None,
    twiml_app_sid: str | None = None,
) -> RuntimeConfig:
    rc = await _get_or_create(db)
    if account_sid is not None:
        rc.twilio_account_sid = account_sid.strip() or None
    if auth_token is not None:
        rc.twilio_auth_token = auth_token.strip() or None
    if api_key_sid is not None:
        rc.twilio_api_key_sid = api_key_sid.strip() or None
    if api_key_secret is not None:
        rc.twilio_api_key_secret = api_key_secret.strip() or None
    if twiml_app_sid is not None:
        rc.twilio_twiml_app_sid = twiml_app_sid.strip() or None
    rc.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(rc)
    return rc


async def get_deepgram_api_key(db: AsyncSession) -> str:
    rc = await _get_or_create(db)
    return (rc.deepgram_api_key or "").strip()


async def set_deepgram_api_key(db: AsyncSession, value: str) -> RuntimeConfig:
    rc = await _get_or_create(db)
    rc.deepgram_api_key = value.strip() or None
    rc.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(rc)
    return rc


async def get_blooio_api_key(db: AsyncSession) -> str:
    rc = await _get_or_create(db)
    return (rc.blooio_api_key or "").strip()


async def set_blooio_api_key(db: AsyncSession, value: str) -> RuntimeConfig:
    rc = await _get_or_create(db)
    rc.blooio_api_key = value.strip() or None
    rc.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(rc)
    return rc


# ============================================================
# Messaging tone / strategic direction (prepended to AI prompts)
# ============================================================

DEFAULT_MESSAGING_DIRECTION = """\
PRIMARY ANGLE — AI findability + GEO (Generative Engine Optimization) + Local SEO.

Backyard pros (pool builders, landscapers, deck builders, outdoor-kitchen
contractors) are quietly losing leads because their site doesn't rank in
Google's AI Overviews, ChatGPT search, Perplexity, or Gemini. When someone
asks "best pool builder near me" the AI summarizes a few competitors — and
if the prospect's site isn't structured for AI to cite, they vanish. This
is the single most important shift in local search in a decade and most
contractors haven't heard the term "GEO" yet.

LEAD WITH THIS. Most outreach should hook on:
  - "Have you checked whether ChatGPT / Perplexity recommends your business
    when someone asks for [their service] in [their city]?"
  - A specific finding from their site (missing schema, no FAQ, no llms.txt,
    site speed / Core Web Vitals issues that hurt AI crawlability)
  - The competitor angle: "Three other [pool builders] in your area are
    showing up in AI Overviews — here's what they have that you don't"

Local SEO (Google Business Profile, review velocity, citation consistency)
is the closely-related secondary angle. AI search and local SEO converge:
the same signals that help you rank in Maps help you get cited by AI.

Tone:
  - Specific. Reference real data. Don't say "we noticed your site has
    issues" — say "your site is missing FAQ schema, which is the #1 thing
    AI engines look for to extract direct answers."
  - Curious, not pitchy. You're a friend who works in marketing flagging
    something useful — not a salesperson hitting quota.
  - Educator's voice. Most contractors don't know what GEO is yet; explain
    it in plain language without talking down.

Avoid:
  - Generic "increase your leads" / "grow your business" framing — too
    abstract for cold outreach
  - "Synergy", "leverage", "optimize", "ROI", "scale" — corporate slop
  - Pitching specific packages or pricing in cold touches; the goal is to
    earn a 15-min conversation, not close a deal
"""


async def get_messaging_direction(db: AsyncSession) -> str:
    """Returns the configured org-wide messaging direction, or the in-code
    default. Used by every email_generator system prompt."""
    rc = await _get_or_create(db)
    val = (rc.messaging_direction or "").strip()
    return val or DEFAULT_MESSAGING_DIRECTION


async def set_messaging_direction(db: AsyncSession, value: str) -> RuntimeConfig:
    """Empty string clears it back to the in-code default."""
    rc = await _get_or_create(db)
    rc.messaging_direction = value.strip() or None
    rc.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(rc)
    return rc


def mask_key(value: str | None) -> str:
    """Show only last 4 chars: 'pk_live_...c82a'"""
    if not value:
        return ""
    v = value.strip()
    if len(v) <= 8:
        return "*" * len(v)
    return f"{v[:8]}...{v[-4:]}"
