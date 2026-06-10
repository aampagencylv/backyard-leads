"""Twilio call reconciliation — fills in Activity rows for calls that
made it through Twilio but never got logged from the browser dialer.

Why this exists: the only path that creates a call Activity today is
twilio_routes.log_call, fired when the rep closes the dialer modal.
If they hang up but skip the outcome modal, navigate away mid-call,
or their browser crashes, no Activity is created. The status webhook
(/voice/status) only updates existing rows — and is also dormant
because the outbound TwiML doesn't set statusCallback on the dial.

Net effect: 6 of Sebastian's 15 calls today are missing from the
timeline. We can't change the team's behavior. We can pull the truth
from Twilio and reconcile every few minutes.

This module: for each rep with twilio_identity, pulls parent-leg calls
(From=client:bmp_user_N) in the last N hours, ensures each has an
Activity row, creating stubs when missing. Idempotent — runs on a
schedule and on demand via a script.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity, Contact, Company, User
from app.services.twilio_voice import TwilioCredentials, TwilioError, normalize_phone_e164
from app.runtime_config import get_twilio_credentials

logger = logging.getLogger("bmp.call_recon")

TWILIO_BASE = "https://api.twilio.com/2010-04-01"


def _outcome_from_status(status: str, duration: int) -> str:
    """Match the same outcome mapping the dialer modal + /voice/status use."""
    if status == "completed":
        return "connected" if duration > 0 else "no_answer"
    if status == "busy":
        return "busy"
    if status in ("failed", "canceled"):
        return "failed"
    if status == "no-answer":
        return "no_answer"
    return ""


async def _twilio_get_calls(creds: TwilioCredentials, *, params: dict) -> list[dict]:
    """Single page from /Calls.json. Caller passes filters."""
    url = f"{TWILIO_BASE}/Accounts/{creds.account_sid}/Calls.json"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params, auth=(creds.account_sid, creds.auth_token))
    if r.status_code != 200:
        raise TwilioError(r.status_code, r.text[:300])
    return r.json().get("calls", []) or []


async def reconcile_calls(db: AsyncSession, *, hours: int = 6) -> dict:
    """Pull parent-leg calls per rep, ensure each has an Activity row.
    Returns counters. Safe to run repeatedly — idempotent on Activity.twilio_call_sid.
    """
    counters = {"reps_checked": 0, "twilio_calls_seen": 0,
                "already_in_db": 0, "stubs_created": 0,
                "skipped_missing_data": 0, "errors": 0}
    creds = await get_twilio_credentials(db)
    if not creds.is_minimally_configured:
        logger.info("call_recon: Twilio not configured — skip")
        return counters

    reps = (await db.execute(
        select(User).where(User.twilio_identity.is_not(None))
    )).scalars().all()
    if not reps:
        return counters

    start_after = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build the union of all PARENT-leg call SIDs Twilio knows about, per rep.
    rep_parent_calls: list[tuple[User, dict]] = []
    for rep in reps:
        if not rep.twilio_identity:
            continue
        try:
            calls = await _twilio_get_calls(
                creds,
                params={
                    "From": f"client:{rep.twilio_identity}",
                    "StartTime>": start_after,
                    "PageSize": 200,
                },
            )
        except TwilioError as e:
            counters["errors"] += 1
            logger.warning(f"call_recon: Twilio error for rep {rep.id}: {e}")
            continue
        counters["reps_checked"] += 1
        for c in calls:
            counters["twilio_calls_seen"] += 1
            rep_parent_calls.append((rep, c))

    if not rep_parent_calls:
        return counters

    # Check which SIDs are already in our Activity table — one query, not N.
    parent_sids = [c["sid"] for _rep, c in rep_parent_calls]
    existing = (await db.execute(
        select(Activity.twilio_call_sid).where(Activity.twilio_call_sid.in_(parent_sids))
    )).scalars().all()
    existing_set = set(existing)
    counters["already_in_db"] = len(existing_set)

    # For the missing ones, we need the dialed number (To). That lives on
    # the CHILD leg (the outbound dial). Build a quick lookup: ParentCallSid
    # → To. Pulled in one query per missing parent — cap at a sane page size.
    missing = [(rep, c) for rep, c in rep_parent_calls if c["sid"] not in existing_set]
    if not missing:
        return counters

    # Pull all child legs for these parents in one Twilio call by filtering
    # on ParentCallSid one-at-a-time — Twilio doesn't support OR on
    # ParentCallSid, so this is N queries. With N typically < 20 it's fine.
    parent_to_child: dict[str, dict] = {}
    for rep, parent in missing:
        try:
            children = await _twilio_get_calls(
                creds, params={"ParentCallSid": parent["sid"], "PageSize": 5},
            )
        except TwilioError:
            continue
        if children:
            # Most parent legs have exactly one child (the outbound dial).
            parent_to_child[parent["sid"]] = children[0]

    # Now create stub Activity rows. Wrap the whole batch in one commit.
    for rep, parent in missing:
        sid = parent["sid"]
        child = parent_to_child.get(sid) or {}
        to_number = (child.get("to") or "").strip()
        duration = int(parent.get("duration") or child.get("duration") or 0)
        status = parent.get("status") or child.get("status") or ""
        outcome = _outcome_from_status(status, duration)
        started_at_str = parent.get("date_created") or child.get("date_created")
        # Twilio uses RFC2822 e.g. "Tue, 19 May 2026 19:00:40 +0000"
        try:
            from email.utils import parsedate_to_datetime
            started_at = parsedate_to_datetime(started_at_str) if started_at_str else datetime.now(timezone.utc)
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
        except Exception:
            started_at = datetime.now(timezone.utc)

        # Look up contact + company by dialed number, if we have one.
        contact_id: Optional[int] = None
        company_id: Optional[int] = None
        normalized_to: Optional[str] = normalize_phone_e164(to_number) if to_number else None
        if normalized_to:
            # Contact match first (preferred — links the call to a person)
            ct = (await db.execute(
                select(Contact).where(Contact.phone == normalized_to)
                .order_by(Contact.is_primary.desc(), Contact.id).limit(1)
            )).scalar_one_or_none()
            if ct:
                contact_id = ct.id
                company_id = ct.company_id
            else:
                # Company main-line fallback
                co = (await db.execute(
                    select(Company).where(Company.phone == normalized_to).limit(1)
                )).scalar_one_or_none()
                if co:
                    company_id = co.id

        if not company_id:
            # Orphan call — number isn't in the CRM. We still record it
            # against the rep so the dashboard call-count is accurate.
            # The activity won't appear in any company timeline (those
            # filter by company_id). If Sebastian later adds the number
            # as a contact, the activity stays attached to the user but
            # won't auto-link.
            counters["orphan_recorded"] = counters.get("orphan_recorded", 0) + 1

        mins, secs = divmod(duration, 60)
        dur_str = f" ({mins}:{secs:02d})" if duration else ""
        who = "the prospect"
        if contact_id:
            ct = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
            if ct:
                who = ct.full_name or ct.email or "the prospect"
        elif company_id:
            co = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
            if co:
                who = f"{co.name} main line"
        outcome_word = {"connected": "connected", "no_answer": "no answer",
                        "busy": "line busy", "failed": "failed"}.get(outcome, outcome or "")
        head = f"Called {who} at {normalized_to or 'unknown'}"
        if outcome_word:
            head += f" — {outcome_word}{dur_str}"
        elif dur_str:
            head += dur_str
        summary = f"{head}\n[reconciled from Twilio — dialer modal didn't log]"

        db.add(Activity(
            company_id=company_id,
            contact_id=contact_id,
            user_id=rep.id,
            activity_type="call",
            content=summary,
            twilio_call_sid=sid,
            call_duration_seconds=duration,
            call_direction="outbound",
            call_outcome=outcome,
            metadata_json=json.dumps({
                "logged_via": "reconciliation",
                "twilio_status": status,
                "twilio_started_at": started_at.isoformat(),
            }),
            created_at=started_at,
        ))
        counters["stubs_created"] += 1

    await db.commit()
    if counters["stubs_created"]:
        logger.info(
            f"call_recon: created {counters['stubs_created']} stub Activity rows "
            f"(reps={counters['reps_checked']}, seen={counters['twilio_calls_seen']}, "
            f"already={counters['already_in_db']}, skipped={counters['skipped_missing_data']})"
        )
    return counters


# ────────────────────────────────────────────────────────────────────────────
# Recording attachment — webhook race recovery + periodic sweep
# ────────────────────────────────────────────────────────────────────────────
#
# The recording-complete webhook fires within seconds of hangup, but the
# Activity row is usually created AFTER that — the rep is still typing
# outcome notes into the dialer modal (observed on prod 2026-06-10:
# webhook 16:39:44, Activity 16:39:48). The old webhook dropped the
# RecordingUrl on the floor when no row matched, which is why ~65% of
# calls showed "no recording" while the audio sat in Twilio the whole
# time. Recovery is two-layered:
#   1. The webhook retries the attach in-process for a few minutes
#      (twilio_routes.voice_recording).
#   2. backfill_missing_recordings() runs on the reconciliation tick and
#      pulls recording URLs straight from the Twilio API for any recent
#      call Activity still missing one — covers app restarts, webhook
#      delivery failures, and the historical backlog.


def _looks_like_stub(a: Activity) -> bool:
    c = (a.content or "")
    return "[reconciled from Twilio" in c or c.startswith("Called the prospect at")


async def attach_recording_for_call_sid(
    db: AsyncSession, call_sid: str, recording_url: str,
) -> Optional[int]:
    """Attach a recording URL to the Activity matching this CallSid.

    Picks the non-stub row when reconciliation + log_call both created one,
    moves the URL onto it, and collapses the duplicates. Returns the
    primary Activity id, or None when no row exists yet (caller decides
    whether to retry). Commits on success.
    """
    rows = (await db.execute(
        select(Activity)
        .where(Activity.twilio_call_sid == call_sid)
        .order_by(Activity.id.desc())
    )).scalars().all()
    if not rows:
        return None

    non_stubs = [a for a in rows if not _looks_like_stub(a)]
    primary = non_stubs[0] if non_stubs else rows[0]
    primary.recording_url = recording_url
    for dup in rows:
        if dup.id != primary.id:
            logger.info(
                f"attach_recording: collapsing duplicate activity id={dup.id} "
                f"into primary id={primary.id} for CallSid={call_sid}"
            )
            # Keep the stub's recording if the primary somehow lacks one
            await db.delete(dup)
    await db.commit()
    return primary.id


async def backfill_missing_recordings(
    db: AsyncSession, *, hours: int = 48, limit: int = 40, max_attempts: int = 3,
) -> dict:
    """Sweep recent call Activities that have a CallSid but no recording_url
    and fetch the recording straight from the Twilio API.

    Checks the call's own recordings first, then the parent leg (Dial-verb
    recordings can land on the parent CallSid). Each sid is attempted at
    most `max_attempts` times (tracked in metadata_json) so genuinely
    unrecorded calls — unanswered, recording-off — stop consuming API
    calls after ~15 minutes of sweeps. Newly attached recordings get the
    transcription pipeline kicked off in the background.
    """
    counters = {"checked": 0, "attached": 0, "no_recording": 0, "errors": 0}
    creds = await get_twilio_credentials(db)
    if not creds.is_minimally_configured:
        return counters

    rows = (await db.execute(
        select(Activity)
        .where(
            Activity.activity_type == "call",
            Activity.twilio_call_sid.is_not(None),
            Activity.recording_url.is_(None),
            Activity.created_at > datetime.now(timezone.utc) - timedelta(hours=hours),
            # Give the webhook + its in-process retry first crack.
            Activity.created_at < datetime.now(timezone.utc) - timedelta(minutes=3),
        )
        .order_by(Activity.id.desc())
        .limit(limit * 2)  # headroom: several rows can share a CallSid
    )).scalars().all()

    # One attempt per distinct CallSid; remember the newest row per sid for
    # the attempts bookkeeping.
    newest_by_sid: dict[str, Activity] = {}
    for a in rows:
        newest_by_sid.setdefault(a.twilio_call_sid, a)

    async with httpx.AsyncClient(timeout=20) as client:
        for call_sid, newest in list(newest_by_sid.items())[:limit]:
            meta = {}
            try:
                meta = json.loads(newest.metadata_json) if newest.metadata_json else {}
            except (ValueError, TypeError):
                meta = {}
            if int(meta.get("rec_check_attempts") or 0) >= max_attempts:
                continue
            counters["checked"] += 1

            try:
                r = await client.get(
                    f"{TWILIO_BASE}/Accounts/{creds.account_sid}/Calls/{call_sid}/Recordings.json",
                    auth=(creds.account_sid, creds.auth_token),
                )
                recs = r.json().get("recordings", []) if r.status_code == 200 else []
                if not recs:
                    # Dial-verb recordings can attach to the parent leg.
                    cr = await client.get(
                        f"{TWILIO_BASE}/Accounts/{creds.account_sid}/Calls/{call_sid}.json",
                        auth=(creds.account_sid, creds.auth_token),
                    )
                    parent = (cr.json() or {}).get("parent_call_sid") if cr.status_code == 200 else None
                    if parent:
                        pr = await client.get(
                            f"{TWILIO_BASE}/Accounts/{creds.account_sid}/Calls/{parent}/Recordings.json",
                            auth=(creds.account_sid, creds.auth_token),
                        )
                        recs = pr.json().get("recordings", []) if pr.status_code == 200 else []
            except (httpx.HTTPError, ValueError) as e:
                counters["errors"] += 1
                logger.warning(f"recording backfill: Twilio lookup failed for {call_sid}: {e}")
                continue

            if recs:
                rec_sid = recs[0].get("sid")
                url = f"{TWILIO_BASE}/Accounts/{creds.account_sid}/Recordings/{rec_sid}.mp3"
                activity_id = await attach_recording_for_call_sid(db, call_sid, url)
                if activity_id is not None:
                    counters["attached"] += 1
                    import asyncio as _asyncio
                    from app.services.call_transcription import (
                        transcribe_and_summarize_in_background,
                    )
                    _asyncio.create_task(
                        transcribe_and_summarize_in_background(activity_id))
            else:
                counters["no_recording"] += 1
                meta["rec_check_attempts"] = int(meta.get("rec_check_attempts") or 0) + 1
                newest.metadata_json = json.dumps(meta)

            import asyncio as _asyncio
            await _asyncio.sleep(0.25)  # stay polite to the Twilio API

    await db.commit()
    if counters["attached"]:
        logger.info(f"recording backfill: {counters}")
    return counters
