"""Engagement Engine — lifecycle API.

Mirrors the public surface of the legacy `app.services.sequence_engine`
(start, pause, resume, wake, terminate) but operates on the new
engagement / actions tables. Every caller in the app that used to call
the legacy module now calls these functions instead.

Design goals:
  - Drop-in compatible at the call sites: same argument shapes, same
    return-type semantics (ints / no-ops) so refactoring is mechanical.
  - One engagement per contact at a time (sequence_number bumps on
    re-enrollment). Idempotent: re-calling start_engagement for a
    contact that already has an active engagement is a no-op returning
    the existing engagement id.
  - Channel resolution via the `channel_types` lookup table. Skip
    evaluation mirrors the legacy `evaluate_skip` — missing email →
    email steps land status='skipped' with `skip_reason` populated so
    BDRs see them on the timeline but they never dispatch.
  - Email + iMessage step bodies pre-generated with the existing
    `generate_cold_email` / `generate_follow_up` / `generate_imessage`
    Claude calls so content quality matches the legacy engine bit-for-
    bit. Failure to pre-generate falls back to body='AUTO:' and the
    dispatcher will regen at send time.
  - Activity row "sequence_created" still written so the CRM timeline,
    morning brief, dashboards, and BDR-app notifications all keep
    working unchanged.
  - `contact.outreach_owner = 'engagement_engine'` is set on every
    start_engagement, so the legacy sequence_engine's skip gate will
    never re-process this contact even if it gets re-enabled.

Legacy compatibility:
  - `company.email_generated = True` is set on enrollment, matching the
    cross-vertical autopilot dedupe gate.
  - Phase transitions ('cold_outreach' / 'declined' / 'dormant') honor
    the DB `enforce_phase_transition` trigger.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import Company, Contact, Activity, User

log = logging.getLogger("engagement_engine.lifecycle")


# ────────────────────────────────────────────────────────────────────────────
# Channel mapping
# ────────────────────────────────────────────────────────────────────────────
# Legacy `step_type` → channel_types.code → channel_types.id (cached).
# 'imessage' maps to 'sms' because the SMSChannel adapter routes iMessage-
# capable numbers through Twilio iMessage and falls back to SMS otherwise.
LEGACY_STEP_TO_CHANNEL_CODE: dict[str, str] = {
    "email":    "email",
    "imessage": "sms",
    "call":     "call_task",
    "linkedin": "linkedin",
    "manual":   "manual",
    "wait":     "wait",
}


_CHANNEL_ID_CACHE: dict[str, int] = {}


async def _channel_id(session: AsyncSession, code: str) -> int:
    """Resolve channel_types.code → channel_types.id, cached after first hit."""
    if code in _CHANNEL_ID_CACHE:
        return _CHANNEL_ID_CACHE[code]
    row = (await session.execute(text(
        "SELECT id FROM channel_types WHERE code = :c"
    ), {"c": code})).first()
    if row is None:
        raise RuntimeError(f"channel_types lookup failed for code={code!r}")
    _CHANNEL_ID_CACHE[code] = int(row[0])
    return _CHANNEL_ID_CACHE[code]


# ────────────────────────────────────────────────────────────────────────────
# Default playbook resolution
# ────────────────────────────────────────────────────────────────────────────

async def _ensure_company_observation(
    db: AsyncSession, *,
    tenant_id: int, company_id: int, contact_id: int,
    website: Optional[str],
) -> None:
    """Auto-seed a website_homepage observation when a company first
    enters the engine. Idempotent — no-op when an observation already
    exists for this company. Failures are silent: observation seeding
    must NEVER block enrollment.

    The signal_watcher polls these on a 14-day cadence (jittered) and
    emits `website_change` signals to the contact's currently-active
    engagement. With this hook in place, every new tenant that enrolls
    contacts via start_engagement gets signal coverage from day one,
    without operator intervention.
    """
    from urllib.parse import urlparse as _urlparse

    if not website:
        return
    raw = (website or "").strip()
    if not raw:
        return
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    try:
        parsed = _urlparse(raw)
        if not parsed.netloc:
            return
    except Exception:
        return
    normalized = raw[:500]

    try:
        await db.execute(text("""
            INSERT INTO observations (
                tenant_id, contact_id, company_id,
                source_type_id, source_url,
                next_poll_at, poll_interval_days,
                is_active, consecutive_failures,
                created_at, updated_at
            )
            SELECT :t, :c, :co,
                   st.id, :url,
                   NOW() + INTERVAL '15 minutes', 14,
                   TRUE, 0,
                   NOW(), NOW()
            FROM source_types st
            WHERE st.code = 'website_homepage'
              AND NOT EXISTS (
                  SELECT 1 FROM observations o
                  WHERE o.company_id = :co
                    AND o.source_type_id = st.id
                    AND o.is_active = TRUE
              )
        """), {"t": tenant_id, "c": contact_id, "co": company_id, "url": normalized})
    except Exception as e:  # noqa: BLE001
        log.warning(
            "_ensure_company_observation failed (silent) tenant=%s company=%s: %s",
            tenant_id, company_id, e,
        )


async def _resolve_default_playbook_id(
    session: AsyncSession, tenant_id: int,
) -> Optional[int]:
    """Return the active playbook id for the tenant. Prefers the playbook
    named '30-day default' (the one the cutover seeded); falls back to
    the first active playbook by id."""
    row = (await session.execute(text("""
        SELECT id FROM playbooks
        WHERE tenant_id = :t AND is_active = TRUE
        ORDER BY
          CASE WHEN name = '30-day default' THEN 0 ELSE 1 END,
          id
        LIMIT 1
    """), {"t": tenant_id})).first()
    return int(row[0]) if row else None


# ────────────────────────────────────────────────────────────────────────────
# Skip evaluation (mirror of legacy sequence_engine.evaluate_skip)
# ────────────────────────────────────────────────────────────────────────────

def _evaluate_skip(contact: Contact, conditions: list[str]) -> Optional[str]:
    """First matching skip reason or None. Same semantics as the legacy
    evaluator so behavior at enrollment time is byte-for-byte equivalent."""
    for cond in conditions or []:
        if cond == "no_email" and not (contact.email or "").strip():
            return "no_email"
        if cond == "no_phone" and not (contact.phone or "").strip():
            return "no_phone"
        if cond == "no_linkedin" and not (contact.linkedin_url or "").strip():
            return "no_linkedin"
        if cond == "opted_out":
            if getattr(contact, "unsubscribed_at", None):
                return "opted_out"
            if getattr(contact, "do_not_text", False):
                return "opted_out"
        if cond == "landline" and getattr(contact, "phone_type", None) == "landline":
            return "landline"
    return None


# ────────────────────────────────────────────────────────────────────────────
# Step body / subject generation (mirror of legacy pre-gen path)
# ────────────────────────────────────────────────────────────────────────────

async def _build_step_payload(
    *, db: AsyncSession, contact: Contact, company: Company,
    tstep: dict, idx: int, email_drafts: dict, imessage_drafts: dict,
) -> tuple[str, str, Optional[str]]:
    """Return (subject, body, task_description) for the given step.

    Strings only — no DB writes. Mirrors the per-step subject/body
    selection in sequence_engine.start_sequence_from_template so the
    legacy `[Skipped] X step N` / `Call talk track` / LinkedIn connect
    note text is preserved.

    Caller-supplied content takes precedence: when `tstep` carries
    explicit `subject` / `body` / `task_description`, those win over
    pre-gen drafts and boilerplate. This is what lets append_steps_to_
    engagement's fallback-into-start_engagement path preserve the BDR's
    manually-composed step instead of overwriting it with generic copy.
    """
    step_type = tstep["step_type"]
    label = tstep["label"]

    explicit_subject = tstep.get("subject")
    explicit_body = tstep.get("body")
    explicit_task = tstep.get("task_description")
    if explicit_subject or explicit_body or explicit_task:
        return (
            explicit_subject or f"{step_type.title()} step {idx}",
            explicit_body or "",
            explicit_task,
        )

    if step_type == "email":
        d = email_drafts.get(label, {})
        return d.get("subject", f"Step {idx}"), d.get("body", "AUTO:"), None

    if step_type == "imessage":
        d = imessage_drafts.get(label, {})
        return (
            f"iMessage step {idx}",
            d.get("body") or "AUTO:",
            None,
        )

    if step_type == "call":
        contact_phone = (contact.phone or "").strip()
        company_phone = (company.phone or "").strip()
        if contact_phone and company_phone and contact_phone != company_phone:
            phone_line = f"📞 Direct: {contact_phone} | Main: {company_phone}\n\n"
        elif contact_phone:
            phone_line = f"📞 {contact_phone}\n\n"
        elif company_phone:
            phone_line = f"📞 Company main line: {company_phone}\n\n"
        else:
            phone_line = ""
        body = (
            f"{phone_line}"
            f"Call talk track:\n\n"
            f"- Hi {contact.first_name or 'there'} — from Backyard Marketing Pros.\n"
            f"- I sent you a note about {company.name} earlier; wanted to catch you live.\n"
            f"- Quick reason for the call: [reference a specific problem from the audit].\n"
            f"- Got 5 min later this week to dig in?\n\n"
            f"If voicemail: short message + send a follow-up email/text the same day."
        )
        return f"Call {idx}", body, body

    if step_type == "linkedin":
        body = (
            f"Connect note (under 280 chars):\n\n"
            f"Hey {contact.first_name or 'there'} — saw your work at {company.name}. "
            f"Love connecting with fellow backyard pros.\n\n"
            f"(After accept) DM with one specific insight from their site/Google reviews."
        )
        return f"LinkedIn step {idx}", body, body

    # 'wait' or 'manual' — no body, BDR fills it in
    return f"{step_type.title()} step {idx}", "", None


async def _pre_generate_drafts(
    *, db: AsyncSession, contact: Contact, company: Company,
    template: list[dict], objective: Optional[str] = None,
) -> tuple[dict, dict, Optional[str]]:
    """Mirror legacy pre-generation: call generate_cold_email,
    generate_follow_up, generate_imessage for the steps that need them.

    Returns (email_drafts, imessage_drafts, audit_url). On any failure,
    drafts dict is partially populated and the dispatcher falls back to
    send-time regeneration (body='AUTO:'). Audit URL failure is silent.
    """
    email_drafts: dict[str, dict] = {}
    imessage_drafts: dict[str, dict] = {}
    audit_url: Optional[str] = None

    try:
        from app.services.audit_report import ensure_audit_for_company
        audit_url = await ensure_audit_for_company(db, company)
    except Exception as e:  # noqa: BLE001
        log.warning("audit pre-gen failed for company %s: %s", company.id, e)

    try:
        from app.runtime_config import get_messaging_direction
        direction = await get_messaging_direction(db)
    except Exception:  # noqa: BLE001
        direction = None

    # Fold the sequence's own agenda into the AI direction so every step in
    # THIS sequence serves it (e.g. a 120-day nurture to stay top-of-mind),
    # on top of the tenant's org-wide messaging.
    if objective and objective.strip():
        _base = (direction or "").strip()
        direction = ((_base + "\n\n") if _base else "") + (
            "THIS SEQUENCE'S AGENDA — every message in this sequence must serve "
            f"this goal:\n{objective.strip()}"
        )

    try:
        problems = json.loads(company.problems_found) if company.problems_found else []
    except (TypeError, ValueError):
        problems = []
    try:
        recent_posts = json.loads(contact.recent_posts_json) if contact.recent_posts_json else []
    except (TypeError, ValueError):
        recent_posts = []

    if contact.email:
        try:
            from app.services.email_generator import generate_cold_email, generate_follow_up
            for tstep in template:
                if tstep["step_type"] != "email":
                    continue
                step_topic = tstep.get("topic") or None
                if tstep["label"] == "cold":
                    draft = await generate_cold_email(
                        business_name=company.name,
                        business_type=company.business_type or company.industry or "backyard professional",
                        website=company.website or "",
                        problems=problems,
                        contact_name=contact.full_name,
                        location=company.city,
                        messaging_direction=direction,
                        topic=step_topic,
                    )
                else:
                    fu_num_map = {"follow_up_1": 1, "follow_up_2": 2, "breakup": 3}
                    fu_num = fu_num_map.get(tstep["label"], 1)
                    cold_subject = email_drafts.get("cold", {}).get("subject", "")
                    draft = await generate_follow_up(
                        business_name=company.name,
                        business_type=company.business_type or company.industry or "backyard professional",
                        problems=problems,
                        previous_email_subject=cold_subject,
                        follow_up_number=fu_num,
                        contact_name=contact.full_name,
                        messaging_direction=direction,
                        audit_url=audit_url,
                        topic=step_topic,
                    )
                email_drafts[tstep["label"]] = draft
        except Exception as e:  # noqa: BLE001
            log.warning("email pre-gen failed for contact %s: %s", contact.id, e)

    if contact.phone:
        try:
            from app.services.email_generator import generate_imessage
            intent_map = {"imessage_1": "after_email", "imessage_2": "follow_up", "imessage_3": "follow_up"}
            for tstep in template:
                if tstep["step_type"] != "imessage":
                    continue
                msg_audit_url = audit_url if tstep["label"] == "imessage_1" else None
                draft = await generate_imessage(
                    business_name=company.name or "your business",
                    business_type=company.business_type or company.industry or "backyard professional",
                    contact_name=contact.full_name,
                    problems=problems,
                    recent_posts=recent_posts,
                    location=(company.city or "") + ((", " + company.state) if company.state else "") or None,
                    intent=intent_map.get(tstep["label"], "follow_up"),
                    messaging_direction=direction,
                    audit_url=msg_audit_url,
                )
                imessage_drafts[tstep["label"]] = draft
        except Exception as e:  # noqa: BLE001
            log.warning("imessage pre-gen failed for contact %s: %s", contact.id, e)

    return email_drafts, imessage_drafts, audit_url


# ────────────────────────────────────────────────────────────────────────────
# Public API: start_engagement
# ────────────────────────────────────────────────────────────────────────────

async def start_engagement(
    db: AsyncSession,
    contact: Contact,
    *,
    template: Optional[list[dict]] = None,
    sequence_label: str = "main",
    objective: Optional[str] = None,
    pre_generate_content: bool = True,
    assigned_bdr_id: Optional[int] = None,
    initiated_by: str = "autopilot",
    hold_for_approval: bool = False,
) -> int:
    """Create an engagement + action rows for the contact.

    hold_for_approval=True (used by 'moderate' campaigns) materializes every
    sendable step as `awaiting_approval` instead of `scheduled`, so NOTHING
    goes out until a BDR approves. One approval on any held step releases the
    whole sequence (see approve_action's cascade). This is the only behavioral
    difference between moderate and full_auto — discovery/enrollment is
    identical for both.

    Returns the number of action rows created. Returns 0 if:
      - contact has no company (orphan)
      - contact is unsubscribed
      - contact.outreach_owner is 'paused' / 'disputed' / 'white_glove'
      - contact already has an active engagement (idempotent no-op)

    On success:
      - sets contact.outreach_owner = 'engagement_engine'
      - sets company.email_generated = True (legacy dedupe signal)
      - writes Activity row "sequence_created"
    """
    if not contact or not contact.company_id:
        return 0

    if getattr(contact, "unsubscribed_at", None):
        return 0

    owner = getattr(contact, "outreach_owner", None) or "engagement_engine"
    if owner in ("paused", "disputed", "white_glove"):
        log.info(
            "start_engagement: skip contact %s (outreach_owner=%s)",
            contact.id, owner,
        )
        return 0

    company = (await db.execute(
        select(Company).where(Company.id == contact.company_id)
    )).scalar_one_or_none()
    if not company:
        return 0

    tenant_id = contact.tenant_id

    # Idempotency: if there's an active engagement already, no-op.
    # Tenant-scope through contacts so a coerced contact_id from another
    # tenant can't pivot through this helper.
    existing = (await db.execute(text("""
        SELECT e.id, e.sequence_number FROM engagements e
        JOIN contacts c ON c.id = e.contact_id
        WHERE e.contact_id = :c
          AND e.status = 'active'
          AND e.tenant_id = c.tenant_id
        ORDER BY e.id DESC LIMIT 1
    """), {"c": contact.id})).first()
    if existing is not None:
        log.info(
            "start_engagement: contact %s already has active engagement id=%s",
            contact.id, existing[0],
        )
        return 0

    # Sequence number bumps on each re-enrollment for the contact.
    max_seq_row = (await db.execute(text("""
        SELECT COALESCE(MAX(e.sequence_number), 0) FROM engagements e
        JOIN contacts c ON c.id = e.contact_id
        WHERE e.contact_id = :c AND e.tenant_id = c.tenant_id
    """), {"c": contact.id})).first()
    sequence_number = (int(max_seq_row[0]) if max_seq_row else 0) + 1

    if template is None:
        # Import lazily to avoid circular imports — sequence_engine imports
        # heavy schedulers that we don't need here.
        from app.services.sequence_engine import DEFAULT_30DAY_TEMPLATE
        template = DEFAULT_30DAY_TEMPLATE

    playbook_id = await _resolve_default_playbook_id(db, tenant_id)
    now = datetime.now(timezone.utc)

    # Pre-generate email + iMessage content with the same Claude calls
    # the legacy engine uses, so subjects/bodies are identical to what
    # autopilot would have produced before the cutover.
    email_drafts: dict = {}
    imessage_drafts: dict = {}
    if pre_generate_content:
        email_drafts, imessage_drafts, _ = await _pre_generate_drafts(
            db=db, contact=contact, company=company, template=template,
            objective=objective,
        )

    # Resolve assigned BDR: explicit param > engagement-level assignment >
    # company.assigned_to. Used by the email channel's reply-to derivation
    # and for the BDR-task channels.
    bdr_id = assigned_bdr_id or getattr(company, "assigned_to", None)

    # INSERT engagement.
    eng_row = (await db.execute(text("""
        INSERT INTO engagements (
            tenant_id, contact_id, company_id,
            sequence_number, current_phase, last_transition_by,
            status, current_action_index,
            engagement_score, tier, summary_version,
            monthly_ai_cost_usd, monthly_ai_cost_reset_at,
            started_at, assigned_bdr_id, objective,
            created_at, updated_at
        )
        VALUES (
            :t, :c, :co,
            :seq, 'cold_outreach', :by,
            'active', 0,
            0, 'cold', 0,
            0, :now,
            :now, :bdr, :objective,
            :now, :now
        )
        RETURNING id
    """), {
        "t": tenant_id,
        "c": contact.id,
        "co": company.id,
        "seq": sequence_number,
        "objective": (objective or None),
        # last_transition_by is VARCHAR(20) on prod — truncate aggressively.
        "by": (initiated_by or "system")[:20],
        "now": now,
        "bdr": bdr_id,
    })).first()
    engagement_id = int(eng_row[0])

    # Materialize action rows from the template.
    created = 0
    earliest_pending: Optional[datetime] = None
    skip_token = secrets.token_hex(4)
    for idx, tstep in enumerate(template, start=1):
        channel_code = LEGACY_STEP_TO_CHANNEL_CODE.get(tstep["step_type"])
        if not channel_code:
            log.warning("start_engagement: unknown step_type %s — skipping", tstep["step_type"])
            continue
        ch_id = await _channel_id(db, channel_code)

        scheduled_at = now + timedelta(days=int(tstep.get("day", 0)))
        stale_after = scheduled_at + timedelta(days=2)

        skip_conds = tstep.get("skip_if", [])
        skip_reason = _evaluate_skip(contact, skip_conds) if skip_conds else None

        subject, body, task_description = await _build_step_payload(
            db=db, contact=contact, company=company,
            tstep=tstep, idx=idx,
            email_drafts=email_drafts, imessage_drafts=imessage_drafts,
        )

        # Recipient fields: the recipient-lock trigger requires these to
        # match the contact's current values OR be NULL. We set them
        # explicitly so the channel adapter has them on hand and the
        # trigger gets to validate at enrollment time.
        recipient_email = contact.email if channel_code == "email" else None
        recipient_phone = contact.phone if channel_code == "sms" else None
        recipient_linkedin = contact.linkedin_url if channel_code == "linkedin" else None

        if skip_reason:
            status = "skipped"
            subject = f"[Skipped] {tstep['step_type'].title()} step {idx}"
            body = f"Skipped at creation: {skip_reason}"
            task_description = None
        else:
            status = "awaiting_approval" if hold_for_approval else "scheduled"
            # Only 'scheduled' actions are due-able; held actions don't set
            # next_action_due_at (the dispatcher claims status='scheduled' only,
            # so they wait until approval flips them to scheduled).
            if status == "scheduled" and (earliest_pending is None or scheduled_at < earliest_pending):
                earliest_pending = scheduled_at

        idem_key = f"enroll-{engagement_id}-{idx}-{skip_token}"

        await db.execute(text("""
            INSERT INTO actions (
                tenant_id, engagement_id, contact_id,
                channel_id, status, requires_human_review,
                scheduled_at, stale_after,
                subject, body, task_description, topic,
                recipient_email, recipient_phone, recipient_linkedin_url,
                idempotency_key, ai_strategy_used,
                skip_reason,
                created_at, updated_at
            )
            VALUES (
                :t, :e, :c,
                :ch, :st, FALSE,
                :sched, :stale,
                :subj, :body, :task, :topic,
                :re, :rp, :rl,
                :idem, 'enrollment',
                :skip,
                :now, :now
            )
            ON CONFLICT (idempotency_key) DO NOTHING
        """), {
            "t": tenant_id, "e": engagement_id, "c": contact.id,
            "ch": ch_id, "st": status,
            "sched": scheduled_at, "stale": stale_after,
            "subj": subject[:255], "body": body, "task": task_description,
            "topic": (tstep.get("topic") or None),
            "re": recipient_email, "rp": recipient_phone, "rl": recipient_linkedin,
            "idem": idem_key, "skip": skip_reason,
            "now": now,
        })
        created += 1

    # Update engagement.next_action_due_at to the earliest scheduled
    # action so the dispatcher can find this engagement efficiently.
    if earliest_pending is not None:
        await db.execute(text("""
            UPDATE engagements SET next_action_due_at = :due WHERE id = :id
        """), {"due": earliest_pending, "id": engagement_id})

    # Stamp the playbook on the engagement if one is configured.
    if playbook_id is not None:
        await db.execute(text("""
            UPDATE engagements
            SET current_playbook_id = :pb, current_playbook_version = 1
            WHERE id = :id
        """), {"pb": playbook_id, "id": engagement_id})

    # Update contact ownership + company dedup flag. Use direct SQL so
    # it works even when the caller passed a Contact/Company that's not
    # attached to this session (e.g. cross-session helpers or scripts).
    await db.execute(text("""
        UPDATE contacts SET outreach_owner = 'engagement_engine' WHERE id = :c
    """), {"c": contact.id})
    await db.execute(text("""
        UPDATE companies SET
            email_generated = TRUE,
            sequence_started_at = COALESCE(sequence_started_at, :now),
            status = CASE WHEN status = 'sequencing' THEN status ELSE 'sequencing' END
        WHERE id = :co
    """), {"co": company.id, "now": now})
    # Also mutate the in-memory objects so subsequent code in the caller
    # sees the new values without an extra reload round-trip.
    contact.outreach_owner = "engagement_engine"
    company.email_generated = True
    if hasattr(company, "sequence_started_at") and company.sequence_started_at is None:
        company.sequence_started_at = now
    if company.status != "sequencing":
        company.status = "sequencing"

    # Activity row for the CRM timeline — same content shape the legacy
    # emitted so dashboards/morning-brief/Kevin's tool surface keep working.
    # user_id = assigned BDR so per-rep activity feeds + audit trails attribute
    # the enrollment to whoever owns the contact (matches legacy behavior).
    db.add(Activity(
        company_id=company.id, contact_id=contact.id,
        user_id=bdr_id,
        activity_type="sequence_created",
        content=f"[engagement engine] Sequence started — {created} steps queued (engagement #{engagement_id})",
    ))

    # Auto-seed a website observation so signal_watcher polls this
    # company's homepage and emits website_change signals to this
    # engagement. Idempotent + silent on failure — never blocks
    # enrollment. Works for every tenant without operator intervention.
    await _ensure_company_observation(
        db,
        tenant_id=tenant_id, company_id=company.id, contact_id=contact.id,
        website=getattr(company, "website", None),
    )

    await db.commit()

    # Fire `contact.enrolled` outbound webhook so subscribers (Zapier ↔
    # Meta Custom Audiences / Google Customer Match / LinkedIn Matched
    # Audiences) get the contact + company info needed for ad audience
    # building. Payload carries every useful match-key field on the
    # contact + company so Zapier filters can decide what to push and
    # the ad platforms have multiple match options.
    #
    # Fired POST-commit so subscribers calling back to query the
    # engagement see committed data.
    try:
        from app.services.webhook_dispatch import dispatch_event

        # Resolve assigned BDR's email + name once for the payload so
        # Zapier can route different reps' contacts to different
        # audiences without an extra API call. NULL if no BDR.
        bdr_email = None
        bdr_name = None
        if bdr_id is not None:
            try:
                bdr_row = (await db.execute(text(
                    "SELECT email, first_name, last_name FROM users WHERE id = :uid"
                ), {"uid": bdr_id})).first()
                if bdr_row:
                    bdr_email = bdr_row.email
                    bdr_name = f"{bdr_row.first_name or ''} {bdr_row.last_name or ''}".strip() or None
            except Exception:
                pass

        contact_full_name = " ".join(
            p for p in [
                getattr(contact, "first_name", None),
                getattr(contact, "last_name", None),
            ] if p
        ) or None

        async with async_session() as ws_db:
            await dispatch_event(ws_db, "contact.enrolled", {
                "tenant_id": tenant_id,
                "engagement_id": engagement_id,
                # The contact object — every match key the ad platforms accept
                "contact": {
                    "id": contact.id,
                    "first_name": getattr(contact, "first_name", None),
                    "last_name": getattr(contact, "last_name", None),
                    "full_name": contact_full_name,
                    "email": getattr(contact, "email", None),
                    "email_status": getattr(contact, "email_status", None),
                    "phone": getattr(contact, "phone", None),
                    "phone_type": getattr(contact, "phone_type", None),
                    "phone_carrier": getattr(contact, "phone_carrier", None),
                    "title": getattr(contact, "title", None),
                    "linkedin_url": getattr(contact, "linkedin_url", None),
                    "timezone": getattr(contact, "timezone", None),
                    "is_primary": bool(getattr(contact, "is_primary", False)),
                    "notes": getattr(contact, "notes", None),
                    # Suppression flags — Zapier filter should respect these
                    "unsubscribed": bool(getattr(contact, "unsubscribed_at", None)),
                    "do_not_text": bool(getattr(contact, "do_not_text", False)),
                    "do_not_contact": bool(getattr(contact, "do_not_contact", False)),
                },
                # The company object — every targeting signal available.
                # NOTE: no postal_code/country columns exist on prod schema;
                # address is a single string + city + state. Country
                # defaults to "US" for SHA-256 hashing where ad platforms
                # require it as a match key.
                "company": {
                    "id": company.id,
                    "name": getattr(company, "name", None),
                    "website": getattr(company, "website", None),
                    "domain": getattr(company, "domain", None),
                    "phone": getattr(company, "phone", None),
                    "address": getattr(company, "address", None),
                    "city": getattr(company, "city", None),
                    "state": getattr(company, "state", None),
                    "country": "US",
                    "business_type": getattr(company, "business_type", None),
                    "industry": getattr(company, "industry", None),
                    "company_size": getattr(company, "company_size", None),
                    "employee_count": getattr(company, "employee_count", None),
                    "founded": getattr(company, "founded", None),
                    "company_description": getattr(company, "company_description", None),
                    # Social profile URLs — useful for LinkedIn Matched
                    # Audiences company match + Meta custom-audience expansion.
                    "linkedin_url": getattr(company, "linkedin_url", None),
                    "facebook_url": getattr(company, "facebook_url", None),
                    "instagram_url": getattr(company, "instagram_url", None),
                    "youtube_url": getattr(company, "youtube_url", None),
                    "tiktok_url": getattr(company, "tiktok_url", None),
                    # Lead score so Zapier can route hot vs cold prospects
                    # to different audiences / campaigns.
                    "lead_score": getattr(company, "lead_score", None),
                    "lead_score_tier": getattr(company, "lead_score_tier", None),
                    # Google Place ID — Meta + Google can geo-match on this.
                    "google_place_id": getattr(company, "google_place_id", None),
                    "rating": getattr(company, "rating", None),
                    "review_count": getattr(company, "review_count", None),
                },
                # Assigned BDR — so audiences can be per-rep or per-territory.
                "assigned_bdr": {
                    "id": bdr_id,
                    "email": bdr_email,
                    "name": bdr_name,
                } if bdr_id else None,
                # Engagement context
                "playbook_id": playbook_id,
                "actions_count": created,
                "started_at": now.isoformat(),
            })
    except Exception as e:  # noqa: BLE001
        log.warning(
            "contact.enrolled webhook dispatch failed (silent) contact=%s: %s",
            contact.id, e,
        )

    log.info(
        "start_engagement: contact=%s engagement=%s actions=%d initiated_by=%s",
        contact.id, engagement_id, created, initiated_by,
    )
    return created


# ────────────────────────────────────────────────────────────────────────────
# pause / resume / terminate
# ────────────────────────────────────────────────────────────────────────────

async def pause_engagement(
    db: AsyncSession,
    contact_id: int,
    *,
    reason: str,
    sequence_label: str = "main",  # accepted for legacy-call-site compatibility
) -> int:
    """Pause every scheduled action belonging to the contact's active
    engagement. Returns the number of actions paused. The engagement
    status stays 'active' so the contact remains in the CRM funnel; we
    only stop outbound dispatch.

    The 'dormant' phase is reserved for system-initiated cool-downs
    (snooze, long inactivity). BDR-initiated pauses (reply received,
    deal stage change) DO NOT transition the phase — that would require
    a phase_transition rule we don't have. They just freeze actions.
    """
    # Tenant-scoped lookup: join through contacts so a body-supplied
    # contact_id from another tenant can't pivot through this helper.
    eng = (await db.execute(text("""
        SELECT e.id FROM engagements e
        JOIN contacts c ON c.id = e.contact_id
        WHERE e.contact_id = :c
          AND e.status = 'active'
          AND e.tenant_id = c.tenant_id
        ORDER BY e.id DESC LIMIT 1
    """), {"c": contact_id})).first()
    if eng is None:
        return 0

    # actions.skip_reason is VARCHAR(80) on prod — truncate to fit. Past
    # the column max we'd hit StringDataRightTruncation and the whole
    # UPDATE fails, so the pause silently drops every action it was
    # supposed to freeze.
    paused_row = await db.execute(text("""
        UPDATE actions
        SET status = 'paused',
            skip_reason = :reason,
            updated_at = NOW()
        WHERE engagement_id = :e
          AND status = 'scheduled'
        RETURNING id
    """), {"e": int(eng[0]), "reason": f"paused: {reason}"[:80]})
    n = len(paused_row.fetchall())

    # Surface the pause on the timeline.
    contact = (await db.execute(
        select(Contact).where(Contact.id == contact_id)
    )).scalar_one_or_none()
    if contact:
        db.add(Activity(
            company_id=contact.company_id, contact_id=contact_id,
            activity_type="sequence_paused",
            content=f"Sequence paused: {reason} ({n} steps frozen)",
        ))

    await db.commit()
    log.info("pause_engagement: contact=%s paused=%d reason=%s", contact_id, n, reason)
    return n


async def resume_engagement(
    db: AsyncSession,
    contact_id: int,
    *,
    sequence_label: str = "main",  # accepted for legacy-call-site compatibility
    resume_at: Optional[datetime] = None,
) -> int:
    """Un-pause future actions. If `resume_at` is given, every paused
    action's scheduled_at is shifted so the earliest paused step lands
    at `resume_at` and the rest preserve their relative offsets."""
    # Normalize resume_at: callers may pass a tz-naive datetime (admin
    # scripts, CSV parses). Compare aware-vs-naive raises TypeError, so
    # treat naive input as UTC.
    if resume_at is not None and resume_at.tzinfo is None:
        resume_at = resume_at.replace(tzinfo=timezone.utc)

    eng = (await db.execute(text("""
        SELECT e.id FROM engagements e
        JOIN contacts c ON c.id = e.contact_id
        WHERE e.contact_id = :c
          AND e.status = 'active'
          AND e.tenant_id = c.tenant_id
        ORDER BY e.id DESC LIMIT 1
    """), {"c": contact_id})).first()
    if eng is None:
        return 0

    if resume_at is not None:
        # Shift schedule by (resume_at - earliest_paused_scheduled). Use
        # `(:shift * INTERVAL '1 second')` instead of `(:shift || ' seconds')::interval`
        # — asyncpg types the bind param as text in the latter and refuses
        # to coerce an int, which crashes the resume entirely.
        earliest = (await db.execute(text("""
            SELECT MIN(scheduled_at) FROM actions
            WHERE engagement_id = :e AND status = 'paused'
        """), {"e": int(eng[0])})).scalar()
        if earliest is not None and earliest < resume_at:
            shift_seconds = int((resume_at - earliest).total_seconds())
            await db.execute(text("""
                UPDATE actions
                SET scheduled_at = scheduled_at + (:shift * INTERVAL '1 second'),
                    stale_after  = stale_after  + (:shift * INTERVAL '1 second')
                WHERE engagement_id = :e AND status = 'paused'
            """), {"e": int(eng[0]), "shift": shift_seconds})

    resumed_row = await db.execute(text("""
        UPDATE actions
        SET status = 'scheduled',
            skip_reason = NULL,
            updated_at = NOW()
        WHERE engagement_id = :e AND status = 'paused'
        RETURNING id
    """), {"e": int(eng[0])})
    n = len(resumed_row.fetchall())

    contact = (await db.execute(
        select(Contact).where(Contact.id == contact_id)
    )).scalar_one_or_none()
    if contact:
        db.add(Activity(
            company_id=contact.company_id, contact_id=contact_id,
            activity_type="sequence_resumed",
            content=f"Sequence resumed ({n} steps un-paused)",
        ))

    await db.commit()
    log.info("resume_engagement: contact=%s resumed=%d", contact_id, n)
    return n


async def terminate_engagement(
    db: AsyncSession,
    contact_id: int,
    *,
    reason: str,
    final_phase: Optional[str] = None,
    transition_by: str = "bdr",
) -> int:
    """Mark the engagement terminal (e.g. unsubscribed, hard bounce,
    deal won/lost, opt-out). Cancels all pending and paused actions.
    Returns number of actions canceled. Idempotent.

    By default the phase is LEFT UNCHANGED — only status flips to
    'terminal'. Callers can request a phase transition via `final_phase`
    if they know the (from_phase, final_phase, transition_by) tuple is
    legal under phase_transitions. The enforce_phase_transition trigger
    only fires when current_phase actually changes, so skipping the
    phase update sidesteps the trigger entirely for the common case
    (closed_won/lost contacts already in cold_outreach get straight to
    status='terminal' without picking a phase that may not be reachable).

    `transition_by` defaults to 'bdr' because the broader phase_transitions
    rule set is available to bdr (e.g. meeting_set→declined is allowed by
    bdr but not by system).
    """
    # Tenant-scope through contacts join.
    eng = (await db.execute(text("""
        SELECT e.id, e.current_phase FROM engagements e
        JOIN contacts c ON c.id = e.contact_id
        WHERE e.contact_id = :c
          AND e.status = 'active'
          AND e.tenant_id = c.tenant_id
        ORDER BY e.id DESC LIMIT 1
    """), {"c": contact_id})).first()
    if eng is None:
        return 0
    engagement_id = int(eng[0])
    current_phase = eng[1]

    canceled_row = await db.execute(text("""
        UPDATE actions
        SET status = 'skipped',
            skip_reason = :reason,
            updated_at = NOW()
        WHERE engagement_id = :e
          AND status IN ('scheduled', 'paused', 'awaiting_approval')
        RETURNING id
    """), {
        "e": engagement_id,
        # actions.skip_reason is VARCHAR(80).
        "reason": f"engagement_terminated:{reason}"[:80],
    })
    n = len(canceled_row.fetchall())

    # Always flip status. Only touch phase if the caller explicitly asks
    # for a transition AND it differs from the current phase. last_transition_by
    # is VARCHAR(20); terminal_reason is VARCHAR(60).
    if final_phase and final_phase != current_phase:
        await db.execute(text("""
            UPDATE engagements
            SET current_phase = :p,
                last_transition_by = :by,
                status = 'terminal',
                terminal_reason = :reason,
                terminal_at = NOW(),
                updated_at = NOW()
            WHERE id = :id
        """), {
            "id": engagement_id, "p": final_phase,
            "by": (transition_by or "bdr")[:20],
            "reason": reason[:60],
        })
    else:
        await db.execute(text("""
            UPDATE engagements
            SET status = 'terminal',
                terminal_reason = :reason,
                terminal_at = NOW(),
                updated_at = NOW()
            WHERE id = :id
        """), {"id": engagement_id, "reason": reason[:60]})

    contact = (await db.execute(
        select(Contact).where(Contact.id == contact_id)
    )).scalar_one_or_none()
    if contact:
        db.add(Activity(
            company_id=contact.company_id, contact_id=contact_id,
            activity_type="sequence_terminated",
            content=f"Sequence terminated: {reason} ({n} pending steps canceled)",
        ))

    await db.commit()
    log.info("terminate_engagement: contact=%s eng=%s canceled=%d reason=%s",
             contact_id, engagement_id, n, reason)
    return n


async def sweep_completed_engagements(db: AsyncSession) -> int:
    """Close out engagements whose every action has finished.

    Engagements previously stayed status='active' forever after the last
    step dispatched — 'in cadence' badges, engagement lists, and AI budget
    allocation all kept treating finished sequences as live work. The
    engagements.status CHECK constraint allows only active/paused/
    hibernating/terminal, so completed-naturally is modeled as
    status='terminal' with terminal_reason='sequence_completed' (the
    terminal-pairing constraint requires terminal_at + terminal_reason
    together). Re-enrollment still works — the start_engagement duplicate
    guard only blocks on status='active'.

    Also flips the company sequencing → contacted when the completed
    engagement was the company's last one with pending work — parity with
    the EmailChannel post-send transition, covering call/manual-final
    sequences that complete via task completion instead of an email send.

    Returns the number of engagements closed. Commits.
    """
    completed = (await db.execute(text("""
        UPDATE engagements e
        SET status = 'terminal',
            terminal_reason = 'sequence_completed',
            terminal_at = NOW(),
            updated_at = NOW()
        WHERE e.status = 'active'
          AND EXISTS (SELECT 1 FROM actions a WHERE a.engagement_id = e.id)
          AND NOT EXISTS (
              SELECT 1 FROM actions a
              WHERE a.engagement_id = e.id
                AND a.status IN ('scheduled', 'paused', 'awaiting_approval'))
        RETURNING e.id, e.company_id
    """))).fetchall()
    if not completed:
        return 0

    company_ids = sorted({int(r.company_id) for r in completed if r.company_id})
    if company_ids:
        await db.execute(text("""
            UPDATE companies co
            SET status = 'contacted'
            WHERE co.id = ANY(:cos)
              AND co.status = 'sequencing'
              AND NOT EXISTS (
                  SELECT 1 FROM engagements e2
                  JOIN actions a2 ON a2.engagement_id = e2.id
                  WHERE e2.company_id = co.id AND e2.status = 'active'
                    AND a2.status IN ('scheduled', 'paused', 'awaiting_approval'))
        """), {"cos": company_ids})

    await db.commit()
    log.info("sweep_completed_engagements: closed %d engagements", len(completed))
    return len(completed)


async def purge_contact_engine_data(db: AsyncSession, contact_id: int) -> int:
    """Clear every row that blocks deleting a contact — engine AND legacy.

    Called BEFORE deleting the contact itself. ALL FKs into contacts are
    NO ACTION (engine tables: actions/engagements/signals/observations;
    legacy: tracking_links/seq_enrollments/activities/tasks/bookings/
    page_views), so without this purge the DELETE raises a foreign-key
    violation (the CRM delete button 500s) — or, where rows linger,
    leaves orphaned scheduled actions that keep dispatching to a contact
    that no longer exists. Engine rows and per-contact analytics are
    deleted; shared history (activities/tasks/bookings) is detached so
    the company timeline survives.

    Deletion order respects the FK graph:
      signals.triggered_action_id → actions; actions.triggered_by_signal_id
      → signals (circular — break it by nulling), then signals → actions →
      observations → engagements, after detaching the two nullable
      referencers (inbound_unattributed, action_dedupe_counters).

    Returns the number of actions deleted (for the caller's audit log).
    Does NOT commit — runs inside the caller's transaction so the purge
    and the contact delete succeed or fail atomically.
    """
    eng_ids = [r[0] for r in (await db.execute(text("""
        SELECT id FROM engagements WHERE contact_id = :c
    """), {"c": contact_id})).fetchall()]

    # Break the signals<->actions FK cycle before deleting either side.
    await db.execute(text("""
        UPDATE actions SET triggered_by_signal_id = NULL,
                           supersedes_action_id = NULL,
                           superseded_by_action_id = NULL
        WHERE contact_id = :c
    """), {"c": contact_id})
    await db.execute(text("""
        UPDATE signals SET triggered_action_id = NULL
        WHERE contact_id = :c
    """), {"c": contact_id})

    if eng_ids:
        await db.execute(text("""
            UPDATE inbound_unattributed SET attributed_engagement_id = NULL
            WHERE attributed_engagement_id = ANY(:e)
        """), {"e": eng_ids})
        await db.execute(text("""
            DELETE FROM action_dedupe_counters WHERE engagement_id = ANY(:e)
        """), {"e": eng_ids})

    await db.execute(text("""
        DELETE FROM signals WHERE contact_id = :c
    """), {"c": contact_id})
    deleted_actions = len((await db.execute(text("""
        DELETE FROM actions WHERE contact_id = :c RETURNING id
    """), {"c": contact_id})).fetchall())
    await db.execute(text("""
        DELETE FROM observations WHERE contact_id = :c
    """), {"c": contact_id})
    await db.execute(text("""
        DELETE FROM engagements WHERE contact_id = :c
    """), {"c": contact_id})

    # LEGACY dependents. Every FK into contacts is NO ACTION (verified via
    # information_schema 2026-06-10), so these block the contact DELETE too.
    # Sentry incident same day: batch delete 500'd because the ORM cascade
    # on generated_emails hit tracking_links.email_id (NO ACTION).
    #
    # Semantics per table:
    #   tracking_links — per-contact/per-email click analytics: DELETE
    #     (must go before the ORM cascades generated_emails away).
    #   seq_enrollments — dead schema, rows would block: DELETE.
    #   activities/tasks/bookings/page_views — shared history that should
    #     SURVIVE on the company timeline / task list: detach (contact_id
    #     NULL, columns are nullable) rather than erase.
    await db.execute(text("""
        DELETE FROM tracking_links
        WHERE contact_id = :c
           OR email_id IN (SELECT id FROM generated_emails WHERE contact_id = :c)
    """), {"c": contact_id})
    await db.execute(text("""
        DELETE FROM seq_enrollments WHERE contact_id = :c
    """), {"c": contact_id})
    for tbl in ("activities", "tasks", "bookings", "page_views"):
        await db.execute(text(
            f"UPDATE {tbl} SET contact_id = NULL WHERE contact_id = :c"
        ), {"c": contact_id})

    log.info("purge_contact_engine_data: contact=%s engagements=%d actions=%d "
             "(+legacy dependents cleared)", contact_id, len(eng_ids), deleted_actions)
    return deleted_actions


# ────────────────────────────────────────────────────────────────────────────
# wake_engagement_for_company — restore enrollment when a BDR un-disqualifies
# a contact or clicks "wake up" in the CRM.
# ────────────────────────────────────────────────────────────────────────────

async def append_steps_to_engagement(
    db: AsyncSession,
    contact: Contact,
    steps: list[dict],
    *,
    strategy_tag: str = "manual_append",
    offset_hours: float = 0.0,
    sequence_label_hint: str = "post_call",
) -> int:
    """Append additional action rows to the contact's active engagement.

    Each step dict should have:
      - day (int) — days from now until scheduled_at
      - step_type ('email' | 'imessage' | 'call' | 'linkedin' | 'manual')
      - subject (str) — body header
      - body (str) — full body / task description
      - skip_if (list[str], optional) — same skip conditions as start_engagement

    `offset_hours` shifts every step's scheduled_at by N hours from now
    in addition to its `day` offset — used by the post-call thank-you
    flow which wants the first step to land ~2h after the call, not
    immediately on the next dispatcher tick.

    Used by post-call follow-up sequences and any other parallel-track
    BDR-initiated sequences. Falls back gracefully if there's no active
    engagement (creates one first via start_engagement). The fallback
    PRESERVES caller-supplied subject/body so manually-composed steps
    are not silently overwritten with generic pre-gen content."""
    if not contact or not contact.company_id:
        return 0

    eng = (await db.execute(text("""
        SELECT e.id FROM engagements e
        JOIN contacts c ON c.id = e.contact_id
        WHERE e.contact_id = :c
          AND e.status = 'active'
          AND e.tenant_id = c.tenant_id
        ORDER BY e.id DESC LIMIT 1
    """), {"c": contact.id})).first()

    if eng is None:
        # No active engagement — start one with these steps as the template.
        # CRITICALLY: pass subject/body/task_description through so the BDR's
        # composed copy survives. pre_generate_content=False because the
        # caller has already produced the content; we don't want generic
        # cold/follow-up Claude calls overwriting it.
        return await start_engagement(
            db, contact,
            template=[
                {
                    "day": s["day"],
                    "step_type": s["step_type"],
                    "label": s.get("label", strategy_tag),
                    "skip_if": s.get("skip_if", []),
                    "subject": s.get("subject"),
                    "body": s.get("body"),
                    "task_description": s.get("task_description"),
                }
                for s in steps
            ],
            sequence_label=sequence_label_hint,
            pre_generate_content=False,
            initiated_by=strategy_tag[:20],
        )

    engagement_id = int(eng[0])
    now = datetime.now(timezone.utc)
    skip_token = secrets.token_hex(4)
    created = 0

    for idx, step in enumerate(steps, start=1):
        channel_code = LEGACY_STEP_TO_CHANNEL_CODE.get(step["step_type"])
        if not channel_code:
            continue
        ch_id = await _channel_id(db, channel_code)
        scheduled_at = (
            now
            + timedelta(days=int(step.get("day", 0)))
            + timedelta(hours=float(offset_hours))
        )
        stale_after = scheduled_at + timedelta(days=2)
        recipient_email = contact.email if channel_code == "email" else None
        recipient_phone = contact.phone if channel_code == "sms" else None
        recipient_linkedin = contact.linkedin_url if channel_code == "linkedin" else None

        # Honor skip_if at creation time — same semantics as start_engagement.
        # Without this, manually-appended email steps to a contact with no
        # email get enqueued and fail at dispatch instead of being pre-skipped.
        skip_conds = step.get("skip_if", [])
        skip_reason = _evaluate_skip(contact, skip_conds) if skip_conds else None
        if skip_reason:
            status = "skipped"
            subject_value = f"[Skipped] {step['step_type'].title()} step {idx}"
            body_value = f"Skipped at creation: {skip_reason}"
        else:
            status = "scheduled"
            subject_value = step.get("subject") or f"{step['step_type'].title()} step {idx}"
            body_value = step.get("body") or ""

        idem_key = f"append-{engagement_id}-{idx}-{skip_token}"

        await db.execute(text("""
            INSERT INTO actions (
                tenant_id, engagement_id, contact_id,
                channel_id, status, requires_human_review,
                scheduled_at, stale_after,
                subject, body,
                recipient_email, recipient_phone, recipient_linkedin_url,
                idempotency_key, ai_strategy_used,
                skip_reason,
                created_at, updated_at
            )
            VALUES (
                :t, :e, :c,
                :ch, :st, FALSE,
                :sched, :stale,
                :subj, :body,
                :re, :rp, :rl,
                :idem, :strategy,
                :skip,
                :now, :now
            )
            ON CONFLICT (idempotency_key) DO NOTHING
        """), {
            "t": contact.tenant_id, "e": engagement_id, "c": contact.id,
            "ch": ch_id, "st": status,
            "sched": scheduled_at, "stale": stale_after,
            "subj": subject_value[:255], "body": body_value,
            "re": recipient_email, "rp": recipient_phone, "rl": recipient_linkedin,
            "idem": idem_key, "strategy": strategy_tag[:40],
            "skip": (skip_reason or None),
            "now": now,
        })
        created += 1

    await db.commit()
    log.info(
        "append_steps_to_engagement: contact=%s engagement=%s appended=%d tag=%s",
        contact.id, engagement_id, created, strategy_tag,
    )
    return created


async def wake_engagement_for_company(
    db: AsyncSession,
    company: Company,
    *,
    initiated_by: str = "bdr_wake",
) -> int:
    """For each contact at the company whose latest engagement is
    terminal (declined) or who has no engagement at all, restart a
    fresh engagement. Returns number of contacts re-enrolled.

    Used by:
      - Restore-from-disqualified flow (company_routes.py)
      - Manual "wake sequence" button on the company page
    """
    contacts = (await db.execute(
        select(Contact).where(Contact.company_id == company.id)
    )).scalars().all()

    re_enrolled = 0
    for c in contacts:
        latest = (await db.execute(text("""
            SELECT status FROM engagements
            WHERE contact_id = :c ORDER BY id DESC LIMIT 1
        """), {"c": c.id})).first()
        # If active: skip (don't re-enroll). If terminal or absent: enroll.
        if latest is not None and latest[0] == "active":
            continue

        # The legacy outreach_owner gate must allow re-enrollment.
        owner = getattr(c, "outreach_owner", None) or "none"
        if owner in ("paused", "disputed", "white_glove"):
            continue

        actions_created = await start_engagement(
            db, c,
            initiated_by=initiated_by,
            assigned_bdr_id=getattr(company, "assigned_to", None),
        )
        if actions_created > 0:
            re_enrolled += 1

    return re_enrolled


async def regenerate_action_if_auto(db: AsyncSession, action_id: int) -> bool:
    """Send-time content generation for the DEFERRED path (bulk apply-to-
    existing leaves email/imessage bodies as 'AUTO:'). Writes real content from
    the action's stored `topic` + the engagement's `objective` + the tenant's
    messaging_direction, so the agenda/topic drive the email no matter which
    enrollment path created it. No-op when the body is already real. Caller
    must have stamped db.info['tenant_id'] so messaging_direction + the
    contact/company lookups resolve to the right tenant. Returns True if it
    rewrote the action."""
    import json as _json
    from sqlalchemy import text as _text, select as _select
    a = (await db.execute(_text("""
        SELECT a.id, a.engagement_id, a.contact_id, a.subject, a.body, a.topic,
               a.scheduled_at, ct.code AS channel_code, e.objective, e.company_id
        FROM actions a
        JOIN channel_types ct ON ct.id = a.channel_id
        JOIN engagements e ON e.id = a.engagement_id
        WHERE a.id = :id
    """), {"id": action_id})).first()
    if not a:
        return False
    body = (a.body or "").strip()
    if body and not body.upper().startswith("AUTO:"):
        return False  # already has real content
    if a.channel_code not in ("email", "imessage", "sms"):
        return False

    from app.models import Contact, Company
    contact = (await db.execute(_select(Contact).where(Contact.id == a.contact_id))).scalar_one_or_none()
    company = (await db.execute(_select(Company).where(Company.id == a.company_id))).scalar_one_or_none()
    if not contact or not company:
        return False

    try:
        from app.runtime_config import get_messaging_direction
        direction = await get_messaging_direction(db)
    except Exception:
        direction = None
    if a.objective and a.objective.strip():
        _base = (direction or "").strip()
        direction = ((_base + "\n\n") if _base else "") + (
            "THIS SEQUENCE'S AGENDA — every message in this sequence must serve "
            f"this goal:\n{a.objective.strip()}"
        )

    try:
        problems = _json.loads(company.problems_found) if company.problems_found else []
    except (TypeError, ValueError):
        problems = []
    topic = a.topic or None

    # Position among this engagement's email steps → cold vs follow-up #.
    pos = (await db.execute(_text("""
        SELECT COUNT(*) FROM actions
        WHERE engagement_id = :e
          AND channel_id IN (SELECT id FROM channel_types WHERE code='email')
          AND scheduled_at < :s AND id != :id
    """), {"e": a.engagement_id, "s": a.scheduled_at, "id": a.id})).scalar() or 0

    subj, bod = a.subject, None
    biz_type = company.business_type or company.industry or "business"
    if a.channel_code == "email":
        from app.services.email_generator import generate_cold_email, generate_follow_up
        if pos == 0:
            d = await generate_cold_email(
                business_name=company.name, business_type=biz_type,
                website=company.website or "", problems=problems,
                contact_name=contact.full_name, location=company.city,
                messaging_direction=direction, topic=topic)
        else:
            d = await generate_follow_up(
                business_name=company.name, business_type=biz_type,
                problems=problems, previous_email_subject="",
                follow_up_number=min(int(pos), 3), contact_name=contact.full_name,
                messaging_direction=direction, topic=topic)
        subj, bod = d.get("subject"), d.get("body")
    else:  # imessage / sms
        from app.services.email_generator import generate_imessage
        try:
            recent = _json.loads(contact.recent_posts_json) if contact.recent_posts_json else []
        except (TypeError, ValueError):
            recent = []
        d = await generate_imessage(
            business_name=company.name or "your business", business_type=biz_type,
            contact_name=contact.full_name, problems=problems, recent_posts=recent,
            location=(company.city or "") + ((", " + company.state) if company.state else "") or None,
            intent="follow_up", messaging_direction=direction)
        subj, bod = (a.subject or "Message"), d.get("body")

    if not (bod or "").strip():
        return False
    await db.execute(_text("UPDATE actions SET subject = :s, body = :b WHERE id = :id"),
                     {"s": (subj or "")[:255], "b": bod, "id": a.id})
    return True
