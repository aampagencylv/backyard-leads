"""
Inbound email reply receiver — Resend Inbound webhook.

Replaces the planned Missive Phase 1 webhook with a token-based scheme that
works regardless of which inbox tool the BDR uses (Missive, Gmail, Outlook,
Spark — anything). Architecture:

  Outbound:  Reply-To: r-<token>@inbound.bymp.com  (per email)
  Reply:     Recipient hits reply → Resend Inbound catches → POSTs here
  Here:      Parse token from To, look up GeneratedEmail, log reply Activity,
             auto-pause sequence, bump company status, then forward the
             message to the BDR's actual inbox so they handle the human side
             in their normal tool.

Token format: r-{27 url-safe chars}@inbound.bymp.com.
Catch-all routing means anything that lands at this domain hits us, including
auto-responders, bounces, and out-of-office messages — all valuable signals
that today drop on the floor.

Public endpoint (no auth — Resend posts here). Verified via signing secret
(when configured) — Resend Inbound supports webhook signatures the same way
their outbound webhooks do.
"""
from __future__ import annotations
import json
import logging
import hmac
import hashlib
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import Response, JSONResponse
from sqlalchemy import select

from app.database import async_session
from app.models import GeneratedEmail, Contact, Company, Activity, User
from app.config import settings
from app.services.email_sender import send_email

router = APIRouter(prefix="/api/email", tags=["inbound"])
log = logging.getLogger("inbound_email")


_TOKEN_RE = re.compile(r"^r-([A-Za-z0-9_-]{20,40})$", re.IGNORECASE)


def _extract_reply_token(to_addresses: list[str]) -> Optional[str]:
    """Find our token in the To/Cc address list. Resend's payload has a
    list of recipients — the prospect may have CC'd or BCC'd extra people,
    so we scan all. Token format `r-<27chars>` in the local-part."""
    for addr in to_addresses or []:
        if not addr:
            continue
        local = addr.split("@", 1)[0].strip().lower()
        m = _TOKEN_RE.match(local)
        if m:
            return m.group(1)
    return None


async def _fetch_inbound_body(email_id: str) -> dict:
    """Fetch the full inbound email content via Resend's API.

    Resend's email.received webhook payload contains metadata only (from /
    to / subject / message_id / attachments) — NOT the body. We pull the
    body separately via GET /emails/inbound/{id} which returns text + html
    + headers + raw mime. Returns {} on any failure (we still process the
    metadata-only path, just with empty body)."""
    if not email_id or not settings.resend_api_key:
        return {}
    import httpx
    url = f"https://api.resend.com/emails/inbound/{email_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {settings.resend_api_key}"})
        if r.status_code != 200:
            log.warning(f"[inbound] body fetch {r.status_code}: {r.text[:200]}")
            return {}
        return r.json() or {}
    except Exception as e:
        log.exception(f"[inbound] body fetch failed: {e}")
        return {}


async def _resolve_resend_webhook_secret() -> str:
    """DB-first lookup with env fallback so Steve can rotate from Settings UI
    without SSHing in. We do this in an async helper instead of a sync read so
    the runtime_config table query plays nicely with FastAPI's event loop."""
    try:
        from app.runtime_config import get_resend_webhook_secret
        async with async_session() as db:
            return await get_resend_webhook_secret(db)
    except Exception:
        # Bootstrap path during initial deploy when the column might not yet
        # exist — fall back to env so the webhook still works.
        return (settings.resend_webhook_secret or "").strip()


def _verify_signature(raw_body: bytes, headers: dict, secret: str) -> bool:
    """Verify a Resend webhook using the Svix Python library — the canonical
    implementation Resend's docs recommend. Handles all the edge cases
    (key rotation, multiple sigs, timestamp tolerance, secret decoding) so
    we don't have to maintain our own crypto code.

    Returns True if no secret configured (bootstrap mode), so the endpoint
    works during early setup before the user has pasted a secret."""
    if not secret:
        return True
    try:
        from svix.webhooks import Webhook  # type: ignore
    except ImportError:
        log.error("[inbound] svix library not installed — accepting unverified")
        return True
    try:
        wh = Webhook(secret)
        # svix expects the standard svix-* headers + the raw body. It raises
        # WebhookVerificationError on any mismatch (signature, timestamp, etc).
        wh.verify(raw_body, headers)
        return True
    except Exception as e:
        log.warning(f"[inbound] signature verification failed: {e}")
        return False


async def _route_reply_to_engagement_engine(
    *, action_id: int,
    from_addr: str, bare_from: str,
    subject: str, body_text: str, html_body: str,
    body_preview: str, is_auto_response: bool,
):
    """Attribute an inbound reply to a new-engine action. Writes:
      - signals.email_reply (the prospect's response is now an AI-actionable signal)
      - actions.outcome='replied' + outcome_observed_at=NOW()
      - engagements.last_reply_at=NOW() (triggers stale-action detection on
        future pending actions for this contact — so we don't keep sending
        if they've replied)
      - Activity row for legacy CRM timeline visibility
    """
    import json as _json
    from sqlalchemy import text as _sa_text
    async with async_session() as db:
        # Resolve action → tenant/engagement/contact/company
        a_row = await db.execute(_sa_text("""
            SELECT a.id, a.tenant_id, a.engagement_id, a.contact_id,
                   e.company_id, e.contact_id AS eng_contact_id
            FROM actions a
            JOIN engagements e ON e.id = a.engagement_id
            WHERE a.id = :id
        """), {"id": action_id})
        a = a_row.first()
        if a is None:
            log.warning(f"[inbound] new-engine token references missing action_id={action_id}; silent drop")
            return {"ok": True, "ignored": "unknown_action"}

        # signal_type_id for email_reply
        st_row = await db.execute(_sa_text(
            "SELECT id FROM signal_types WHERE code = 'email_reply'"
        ))
        st = st_row.first()

        # Insert signal — idempotent by Resend message id (when available)
        # else a hash of the subject+body for dedupe.
        idem_hash = abs(hash(f"{action_id}|{subject}|{body_text[:200]}")) % (10 ** 12)
        idem = f"reply-{action_id}-{idem_hash}"
        raw_data = {
            "from": bare_from,
            "subject": subject,
            "preview": body_preview,
            "is_auto_response": is_auto_response,
            # Body in untrusted_content per Rule #12 — the decision_maker
            # will wrap it before feeding to any LLM.
            "body_text": body_text[:8192],
        }
        await db.execute(_sa_text("""
            INSERT INTO signals (
                tenant_id, engagement_id, contact_id, signal_type_id,
                raw_data_json, observed_at, idempotency_key,
                is_untrusted_content
            )
            VALUES (:t, :eng, :c, :st,
                    CAST(:raw AS jsonb), NOW(), :idem, TRUE)
            ON CONFLICT (idempotency_key) DO NOTHING
        """), {
            "t": a.tenant_id, "eng": a.engagement_id, "c": a.contact_id,
            "st": st.id if st else None,
            "raw": _json.dumps(raw_data, default=str),
            "idem": idem,
        })

        # Update the action's outcome
        await db.execute(_sa_text("""
            UPDATE actions
            SET outcome = COALESCE(outcome, :o),
                outcome_observed_at = COALESCE(outcome_observed_at, NOW())
            WHERE id = :id
        """), {"id": action_id,
               "o": "auto_response" if is_auto_response else "replied"})

        # Update engagement.last_reply_at so the dispatcher's stale-action
        # check blocks any further pending sends for this contact.
        if not is_auto_response:
            await db.execute(_sa_text("""
                UPDATE engagements
                SET last_reply_at = NOW()
                WHERE id = :id
            """), {"id": a.engagement_id})

        # Look up engagement BDR for per-rep reply attribution. Pre-fix
        # this was NULL → dashboards attributed via company.assigned_to
        # which mis-counts when engagement.assigned_bdr_id ≠ company.
        # assigned_to (cross-rep coverage / round-robin churn cases).
        bdr_lookup = await db.execute(_sa_text("""
            SELECT COALESCE(e.assigned_bdr_id, co.assigned_to) AS bdr_id
            FROM engagements e
            JOIN companies co ON co.id = e.company_id
            WHERE e.id = :e
        """), {"e": a.engagement_id})
        bdr_row = bdr_lookup.first()
        engine_bdr_id = bdr_row.bdr_id if bdr_row else None

        # Legacy CRM timeline visibility — keeps the existing reply UI working
        activity_type = "email_auto_response" if is_auto_response else "email_replied"
        prefix = "[Auto-response]" if is_auto_response else "[Reply]"
        db.add(Activity(
            company_id=a.company_id,
            contact_id=a.contact_id,
            user_id=engine_bdr_id,  # per-rep attribution: who owns this engagement
            activity_type=activity_type,
            content=f"{prefix} {subject or '(no subject)'} — {body_preview or '(empty body)'}",
            metadata_json=_json.dumps({
                "from": bare_from,
                "from_raw": from_addr,
                "subject": subject,
                "preview": body_preview,
                "body_text": body_text,
                "body_html": html_body,
                "engagement_action_id": action_id,
                "is_auto_response": is_auto_response,
                "engine": "engagement_engine",
            }),
        ))

        # POST-CUTOVER reply side-effects parity with legacy path:
        #   1. Flip company.status='replied' (drives the Companies filter
        #      pill + the Missive label auto-sync). Skip auto-responses
        #      so vacation OOOs don't flip status.
        #   2. Resolve BDR + forward to their inbox so they see the reply
        #      in Missive/Gmail alongside the original outbound.
        #   3. Fire the email.replied outbound webhook so customer
        #      Zapier/Make flows tied to replies still trigger.
        # All three were live for legacy emails; engine emails got NONE
        # until now. Steve found this when 'Replied' tab stayed empty
        # despite obvious replies landing.
        bdr_id = None
        co_obj = None
        contact_obj = None
        if not is_auto_response:
            # status='replied' guarded so we don't downgrade qualified/converted
            await db.execute(_sa_text("""
                UPDATE companies
                SET status = 'replied'
                WHERE id = :co AND status IN ('new','pursuing','sequencing','contacted')
            """), {"co": a.company_id})

            # Resolve BDR + the orm objects forward needs
            ctx = await db.execute(_sa_text("""
                SELECT COALESCE(e.assigned_bdr_id, co.assigned_to) AS bdr_id
                FROM engagements e
                JOIN companies co ON co.id = e.company_id
                WHERE e.id = :e
            """), {"e": a.engagement_id})
            r = ctx.first()
            bdr_id = r.bdr_id if r else None
            # Fallback so a prospect reply is NEVER lost: if the contact has no
            # assigned rep (autopilot-enrolled, freshly imported, etc.), forward
            # to the tenant's senior active admin so someone's business inbox
            # still gets it.
            if bdr_id is None:
                fb = await db.execute(_sa_text("""
                    SELECT id FROM users
                    WHERE tenant_id = :t AND is_active = TRUE
                      AND email IS NOT NULL AND email <> ''
                    ORDER BY CASE role WHEN 'super_admin' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, id
                    LIMIT 1
                """), {"t": a.tenant_id})
                fbr = fb.first()
                bdr_id = fbr.id if fbr else None
            from app.models import Company as _Company, Contact as _Contact, User as _User
            co_obj = (await db.execute(
                select(_Company).where(_Company.id == a.company_id)
            )).scalar_one_or_none()
            contact_obj = (await db.execute(
                select(_Contact).where(_Contact.id == a.contact_id)
            )).scalar_one_or_none()

        await db.commit()

        # Forward + webhook AFTER commit so a failure in either doesn't
        # roll back the reply-attribution work.
        if not is_auto_response and bdr_id is not None:
            try:
                from app.models import User as _User
                async with async_session() as bdr_db:
                    bdr_user = (await bdr_db.execute(
                        select(_User).where(_User.id == bdr_id)
                    )).scalar_one_or_none()
                if bdr_user and bdr_user.email:
                    await _forward_to_bdr(
                        sender_user=bdr_user,
                        prospect_email=bare_from,
                        prospect_name=(contact_obj.full_name if contact_obj else "") or bare_from,
                        subject=subject or "(no subject)",
                        body_text=body_text or "",
                        body_html=html_body or "",
                        contact=contact_obj,
                        company=co_obj,
                        inbound_id=None,
                        db=None,
                    )
            except Exception as e:
                log.warning(f"[inbound:engine] BDR forward failed for action={action_id}: {e}")

        if not is_auto_response:
            try:
                from app.services.webhook_dispatch import dispatch_event
                async with async_session() as wh_db:
                    await dispatch_event(wh_db, "email.replied", {
                        "tenant_id": a.tenant_id,
                        "engagement_action_id": action_id,
                        "engagement_id": a.engagement_id,
                        "company_id": a.company_id,
                        "company_name": co_obj.name if co_obj else None,
                        "contact_id": a.contact_id,
                        "contact_email": bare_from,
                        "contact_name": contact_obj.full_name if contact_obj else None,
                        "subject": subject,
                        "preview": body_preview,
                        "engine": "engagement_engine",
                    })
            except Exception as e:
                log.warning(f"[inbound:engine] email.replied webhook dispatch failed for action={action_id}: {e}")

    # Fire async sentiment classification for real replies. We don't await
    # — the AI roundtrip should not block the webhook ack. The classifier
    # writes the result onto BOTH the signal's raw_data_json (so the
    # decision_maker + lead_scorer see it) AND any Activity rows for this
    # contact created in the last 60 seconds (so the CRM timeline badges
    # show the right color without a refresh delay). Idempotent.
    if not is_auto_response and (body_text or "").strip():
        import asyncio as _asyncio
        _asyncio.create_task(_classify_engagement_reply_async(
            tenant_id=a.tenant_id,
            engagement_id=a.engagement_id,
            contact_id=a.contact_id,
            action_id=action_id,
            body_text=body_text,
            subject=subject,
            channel="email",
        ))

    log.info(
        f"[inbound:engine] action={action_id} from={bare_from} "
        f"auto={is_auto_response}"
    )
    return {"ok": True, "engine": "engagement_engine", "action_id": action_id}


async def _classify_engagement_reply_async(
    *,
    tenant_id: int,
    engagement_id: int,
    contact_id: int,
    action_id: int | None,
    body_text: str,
    subject: str,
    channel: str = "email",
) -> None:
    """Classify the reply intent and persist the result onto the
    matching engagement-engine signal + any recent Activity row.

    Best-effort: silent on failure (the signal simply stays without a
    sentiment field; lead_scorer + decision_maker treat that as
    'unclassified', which is the same as today's no-op behavior).

    Channel: 'email' or 'sms' — both classify with the same Haiku model
    via reply_classifier.classify_reply since the sentiment buckets
    apply equally to short SMS replies.
    """
    try:
        from app.services.reply_classifier import classify_reply
        from app.database import async_session as _async_session
        from sqlalchemy import text as _sa_text
        import json as _json

        result = await classify_reply(body_text, subject)
        if not result:
            return

        sentiment = result.get("sentiment")
        summary = (result.get("summary") or "").strip()[:200]
        if not sentiment:
            return

        signal_code = "email_reply" if channel == "email" else "sms_reply"

        async with _async_session() as db:
            # Patch the most-recent matching signal for this contact —
            # jsonb_set merges so we don't clobber the existing payload.
            # Window is 5 minutes back to be safe on slow webhook acks.
            # NOTE the ::text casts — :sentiment binds twice inside
            # jsonb_build_object and asyncpg can't infer the parameter
            # type without them (IndeterminateDatatypeError; every
            # classification failed until 2026-06-10).
            await db.execute(_sa_text("""
                UPDATE signals
                SET raw_data_json = COALESCE(raw_data_json, '{}'::jsonb)
                                    || jsonb_build_object(
                                        'sentiment',         :sentiment ::text,
                                        'reply_sentiment',   :sentiment ::text,
                                        'summary',           :summary ::text
                                    )
                WHERE id = (
                    SELECT s.id FROM signals s
                    JOIN signal_types st ON st.id = s.signal_type_id
                    WHERE s.tenant_id = :t
                      AND s.contact_id = :c
                      AND st.code = :code
                      AND s.observed_at >= NOW() - INTERVAL '5 minutes'
                    ORDER BY s.observed_at DESC
                    LIMIT 1
                )
            """), {
                "t": tenant_id, "c": contact_id,
                "code": signal_code,
                "sentiment": sentiment,
                "summary": summary,
            })

            # Also stamp Activity rows for the same contact in the same
            # window (legacy CRM timeline badges).
            await db.execute(_sa_text("""
                UPDATE activities
                SET reply_sentiment = :sentiment,
                    reply_sentiment_summary = :summary
                WHERE contact_id = :c
                  AND created_at >= NOW() - INTERVAL '5 minutes'
                  AND activity_type IN ('email_replied', 'sms_inbound', 'email_auto_response')
                  AND reply_sentiment IS NULL
            """), {
                "c": contact_id,
                "sentiment": sentiment,
                "summary": summary,
            })

            # ENFORCE explicit opt-outs. Classification previously only
            # painted a badge — a prospect replying "stop emailing me" kept
            # getting the rest of the sequence. do_not_contact blocks the
            # engine (kill-switch gate); unsubscribed_at blocks the legacy
            # send routes; terminate_engagement cancels scheduled steps.
            if sentiment == "unsubscribe":
                await db.execute(_sa_text("""
                    UPDATE contacts
                    SET do_not_contact = TRUE,
                        unsubscribed_at = COALESCE(unsubscribed_at, NOW())
                    WHERE id = :c
                """), {"c": contact_id})
                await db.commit()
                try:
                    from app.engagement_engine.lifecycle import terminate_engagement
                    await terminate_engagement(
                        db, contact_id, reason="reply_unsubscribe",
                        transition_by="system",
                    )
                except Exception as _te:
                    log.warning("[classify] terminate after unsubscribe failed "
                                "(contact=%s): %s", contact_id, _te)
                log.info("[classify] contact=%s opted out via reply — "
                         "do_not_contact set, engagement terminated", contact_id)

            await db.commit()

        # POST-CUTOVER: now that the sentiment is known, force a lead-
        # score recompute. Pre-fix, lead_scorer ran when the Activity
        # row first landed (sentiment NULL → REPLY_SENTIMENT_WEIGHTS
        # bucket was the default ~20). After the classifier updated
        # the column nothing re-ran scoring, so 'interested' replies
        # never got the +80 boost they should have. Fire-and-forget
        # in its own session so a failure doesn't fail this function.
        try:
            from app.services.lead_scorer import get_or_recompute
            from app.models import Company as _Company
            async with async_session() as ls_db:
                co = (await ls_db.execute(
                    select(_Company).join(
                        Contact, Contact.company_id == _Company.id
                    ).where(Contact.id == contact_id)
                )).scalar_one_or_none()
                if co is not None:
                    await get_or_recompute(ls_db, co, force=True)
        except Exception as _le:
            log.warning(
                "[classify] post-classify lead-score recompute failed "
                "(contact=%s): %s", contact_id, _le,
            )

        log.info(
            "[classify] contact=%s channel=%s sentiment=%s",
            contact_id, channel, sentiment,
        )
    except Exception as e:
        log.warning(
            "[classify] async classification failed (contact=%s channel=%s): %s",
            contact_id, channel, e,
        )


@router.post("/inbound")
async def email_inbound(request: Request):
    """Resend Inbound webhook receiver. Public — no auth. Optionally
    HMAC-verified via settings.resend_webhook_secret."""
    raw = await request.body()
    # Pass the full headers dict so the verifier can read svix-id, svix-timestamp,
    # and svix-signature — all three are needed for Svix's HMAC scheme.
    secret = await _resolve_resend_webhook_secret()
    # Lower-case header dict (Starlette already does this internally but be explicit)
    hdrs = {k.lower(): v for k, v in request.headers.items()}
    if not _verify_signature(raw, hdrs, secret):
        return JSONResponse({"ok": False, "error": "bad signature"}, status_code=401)

    try:
        payload = json.loads(raw or b"{}")
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    return await process_inbound_payload(payload)


async def process_inbound_payload(payload: dict):
    """Core inbound-reply processing, callable from BOTH the Resend webhook
    route above AND the pull-based poller (app/services/inbound_email_poller).

    The poller exists because Resend's email.received webhook push never
    delivered a single event despite correct configuration (verified
    2026-06-10: 27 inbound emails sitting in Resend's store — including 12
    real prospect replies dating to May 12 — zero webhook attempts in nginx
    history). Polling GET /emails/inbound is the reliable path; the webhook,
    if it ever starts working, simply gets deduped by the poller's
    ingestion markers.
    """
    event_type = payload.get("type") or ""
    if event_type and event_type != "email.received":
        # Not a received email — could be a delivery / bounce event we don't care about here.
        return {"ok": True, "ignored": event_type}

    data = payload.get("data") or payload  # tolerant — direct payload OR wrapped

    # Resend Inbound webhooks ship METADATA ONLY — the body (html + text) has
    # to be fetched separately via GET /emails/inbound/{email_id}. So step 1
    # is locate the resend email_id from the payload, then fetch the rest.
    resend_inbound_id = data.get("email_id")
    fetched = await _fetch_inbound_body(resend_inbound_id) if resend_inbound_id else {}
    if fetched:
        # Merge body fields onto the data dict so the rest of the handler
        # works as if Resend had sent the body inline.
        if fetched.get("html"): data["html"] = fetched["html"]
        if fetched.get("text"): data["text"] = fetched["text"]

    to_list = data.get("to") or []
    if isinstance(to_list, str):
        to_list = [to_list]
    cc_list = data.get("cc") or []
    if isinstance(cc_list, str):
        cc_list = [cc_list]

    token = _extract_reply_token(to_list + cc_list)
    if not token:
        log.info(f"[inbound] No token in To/Cc — ignoring. To: {to_list}, Cc: {cc_list}")
        return {"ok": True, "ignored": "no_token"}

    from_addr = (data.get("from") or "").strip()
    # Resend may give us 'Name <email>' format — extract the bare email
    bare_from = from_addr
    m = re.search(r"<([^>]+)>", from_addr)
    if m:
        bare_from = m.group(1).strip()

    subject = (data.get("subject") or "").strip()
    text_body = (data.get("text") or data.get("text_body") or "").strip()
    html_body = (data.get("html") or data.get("html_body") or "").strip()
    body_for_log = text_body or _strip_html(html_body)
    # Strip signatures + quoted reply chains for the short preview the timeline
    # shows. Full body is stored separately in metadata_json so the UI can
    # expand it on click.
    stripped = _strip_reply_signature(body_for_log)
    body_preview = stripped[:300] + ("…" if len(stripped) > 300 else "")

    # Auto-responder / bounce detection — common patterns in From or Subject.
    # We still log these but DON'T auto-pause (the human didn't actually engage).
    is_auto_response = _looks_like_auto_response(from_addr, subject, body_for_log)

    # New-engine token detection. Format: `a{action_id}_{hex}`. The "a" prefix +
    # underscore separator distinguishes from legacy tokens (pure hex, no
    # underscore). When detected, route the reply through the engagement engine
    # — create an `email_reply` signal, mark the action's outcome=replied,
    # update engagement.last_reply_at — and short-circuit the legacy path.
    import re as _re
    _new_engine_token_re = _re.compile(r"^a(\d+)_[a-f0-9]+$", _re.IGNORECASE)
    new_engine_match = _new_engine_token_re.match(token or "")
    if new_engine_match:
        action_id = int(new_engine_match.group(1))
        return await _route_reply_to_engagement_engine(
            action_id=action_id,
            from_addr=from_addr, bare_from=bare_from,
            subject=subject, body_text=body_for_log, html_body=html_body,
            body_preview=body_preview, is_auto_response=is_auto_response,
        )

    async with async_session() as db:
        ge = (await db.execute(
            # Case-insensitive lookup: most mail providers normalize email
            # local-parts to lowercase, so old urlsafe-base64 tokens (mixed
            # case) wouldn't match a direct == comparison. New tokens use
            # lowercase hex but we keep the case-insensitive match for
            # back-compat with anything sent before the format switch.
            select(GeneratedEmail).where(
                GeneratedEmail.reply_token.ilike(token)
            )
        )).scalar_one_or_none()
        if not ge:
            log.warning(f"[inbound] Token {token[:8]}… not found in DB — silent drop. From: {bare_from}")
            return {"ok": True, "ignored": "unknown_token"}

        contact = (await db.execute(select(Contact).where(Contact.id == ge.contact_id))).scalar_one_or_none()
        company = (await db.execute(select(Company).where(Company.id == ge.company_id))).scalar_one_or_none()

        # Mine the signature for enrichment data (mobile, office, LinkedIn) BEFORE
        # we strip it for display. Apply found values to the contact ONLY when
        # their existing field is empty — never overwrite something the BDR has
        # already curated.
        sig_data = parse_signature_for_enrichment(body_for_log) if not is_auto_response else {}
        enriched_fields: list[str] = []
        if contact and sig_data:
            # Phone: prefer mobile > any_phone > office. Mobile is the highest
            # value because it unlocks SMS/iMessage.
            new_phone = sig_data.get("mobile_phone") or sig_data.get("any_phone") or sig_data.get("office_phone")
            if new_phone and not (contact.phone or "").strip():
                contact.phone = new_phone
                enriched_fields.append(f"phone={new_phone}")
                # Reset phone-type cache so the next iMessage/SMS attempt does a
                # fresh lookup against this newly-discovered number.
                contact.phone_type = None
                contact.phone_carrier = None
                contact.phone_type_checked_at = None
            if sig_data.get("linkedin_url") and not (contact.linkedin_url or "").strip():
                contact.linkedin_url = sig_data["linkedin_url"]
                enriched_fields.append(f"linkedin={sig_data['linkedin_url']}")

        # Log Activity to the contact's timeline
        activity_type = "email_auto_response" if is_auto_response else "email_replied"
        prefix = "[Auto-response]" if is_auto_response else "[Reply]"
        reply_activity = Activity(
            company_id=ge.company_id,
            contact_id=ge.contact_id,
            activity_type=activity_type,
            content=f"{prefix} {subject or '(no subject)'} — {body_preview or '(empty body)'}",
            metadata_json=json.dumps({
                "from": bare_from,
                "from_raw": from_addr,
                "subject": subject,
                "preview": body_preview,
                "body_text": body_for_log,            # full plaintext for UI expand
                "body_html": html_body,                # full HTML for UI expand (rich render)
                "email_id": ge.id,
                "is_auto_response": is_auto_response,
                "signature_extracted": sig_data,
                "enriched_fields": enriched_fields,
            }),
        )
        db.add(reply_activity)
        await db.flush()  # ensure reply_activity.id is set so the async classifier can find it

        # If we enriched, log a separate enrichment Activity so the BDR sees
        # exactly what got auto-applied. Distinct from the email_replied entry
        # so it surfaces in any "what got changed" feed.
        if enriched_fields:
            db.add(Activity(
                company_id=ge.company_id,
                contact_id=ge.contact_id,
                activity_type="enriched_from_reply",
                content=f"📋 Auto-enriched from reply signature: {', '.join(enriched_fields)}",
                metadata_json=json.dumps({
                    "fields": enriched_fields,
                    "from_reply_email_id": ge.id,
                    "raw_signature_data": sig_data,
                }),
            ))

        # Auto-pause + status bump only on REAL replies.
        # Routes through the engagement engine's lifecycle.pause_engagement
        # which freezes pending action rows. The legacy pause_sequence is
        # kept as a fallback for contacts with paused_at-aware legacy
        # GeneratedEmail rows (back-compat for the cutover window).
        if not is_auto_response:
            from app.engagement_engine.lifecycle import pause_engagement
            try:
                await pause_engagement(
                    db, ge.contact_id,
                    reason=f"prospect replied to '{ge.subject}'",
                )
            except Exception as e:
                log.exception(f"[inbound] pause_engagement failed: {e}")
            # Legacy fallback — no-op for engagement_engine-owned contacts
            # (whose generated_emails are all marked is_sent or paused_at).
            from app.services.sequence_engine import pause_sequence
            try:
                await pause_sequence(
                    db, ge.contact_id,
                    reason=f"prospect replied to '{ge.subject}'",
                    sequence_label=(ge.sequence_label or "main"),
                )
            except Exception as e:
                log.exception(f"[inbound] legacy pause_sequence fallback failed: {e}")
            if company and company.status in ("sequencing", "contacted", "new"):
                company.status = "replied"

        await db.commit()

        # Forward the message to the BDR's actual inbox so they handle the
        # conversation in their normal tool. Skip auto-responses — those don't
        # need human attention, the timeline log is enough.
        if not is_auto_response:
            sender_user_id = ge.sent_by_user_id
            sender_user = None
            if sender_user_id:
                sender_user = (await db.execute(select(User).where(User.id == sender_user_id))).scalar_one_or_none()
            if not sender_user and company and company.assigned_to:
                sender_user = (await db.execute(select(User).where(User.id == company.assigned_to))).scalar_one_or_none()
            if sender_user and sender_user.email:
                try:
                    await _forward_to_bdr(
                        sender_user=sender_user,
                        prospect_email=bare_from,
                        prospect_name=_extract_display_name(from_addr),
                        subject=subject,
                        body_text=text_body,
                        body_html=html_body,
                        contact=contact,
                        company=company,
                        inbound_id=resend_inbound_id,
                        db=db,
                    )
                except Exception as e:
                    log.exception(f"[inbound] forward to BDR failed: {e}")

    # Fire async sentiment classification — classifies the reply, writes
    # sentiment + summary back onto the Activity row. Done out-of-band so
    # the webhook responds fast and the AI call doesn't block other
    # activity logging. Classification cost is metered as ai_reply_classify.
    if reply_activity.id and (body_for_log or "").strip():
        import asyncio as _asyncio
        _asyncio.create_task(_classify_reply_async(
            reply_activity.id, body_for_log, subject,
        ))

    # Outbound webhooks — notify subscribed customer endpoints. Real
    # replies only (skip auto-responses); fire-and-forget.
    if not is_auto_response:
        try:
            from app.services.webhook_dispatch import dispatch_event
            await dispatch_event(db, "email.replied", {
                "activity_id": reply_activity.id,
                "company_id": ge.company_id,
                "contact_id": ge.contact_id,
                "from_email": bare_from,
                "subject": subject,
                "preview": body_preview,
                "received_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass

    return {"ok": True, "token_matched": token[:8] + "…", "auto_response": is_auto_response}


async def _classify_reply_async(activity_id: int, body_text: str, subject: str) -> None:
    """Background: classify a reply and stamp the result on the Activity.

    Opens its own DB session so the caller's transaction can commit without
    waiting on the AI roundtrip. Failure is silent — the Activity simply
    stays with NULL sentiment and the UI shows a neutral badge.
    """
    try:
        from app.services.reply_classifier import classify_reply
        from app.database import async_session
        result = await classify_reply(body_text, subject)
        if not result:
            return
        async with async_session() as db:
            row = (await db.execute(
                select(Activity).where(Activity.id == activity_id)
            )).scalar_one_or_none()
            if not row:
                return
            row.reply_sentiment = result.get("sentiment")
            row.reply_sentiment_summary = result.get("summary") or ""
            await db.commit()
    except Exception as e:
        log.warning(f"[inbound] async classify failed for activity {activity_id}: {e}")


# ============================================================
# Helpers
# ============================================================

_HTML_TAG_RE = re.compile(r"<[^>]+>")
def _strip_html(s: str) -> str:
    return _HTML_TAG_RE.sub("", s or "").strip()


# ============================================================
# Signature stripping + enrichment extraction
#
# Two-pass: keep the full body in metadata for UI expand, BUT extract
# phone / mobile / LinkedIn URL from the signature for contact enrichment
# BEFORE stripping. Steve's whole point — prospects often don't list
# their mobile in their CRM record but do in their email signature, and
# that mobile is the most valuable field to capture for SMS / iMessage.
# ============================================================

# Markers that conventionally signal "the human-written part is over"
_SIGNATURE_DELIMITERS = (
    "\n-- \n", "\n--\n",                           # RFC 3676 sigdash
    "\n_______________________",                   # underscores
    "\n----------",                                 # generic divider
    "Sent from my iPhone", "Sent from my iPad", "Sent from my Phone",
    "Sent from Outlook", "Sent from Gmail", "Sent from Yahoo Mail",
    "Get Outlook for", "Get the Outlook app",
    "On Mon", "On Tue", "On Wed", "On Thu", "On Fri", "On Sat", "On Sun",  # quoted reply chain
    "On May", "On Jan", "On Feb", "On Mar", "On Apr", "On Jun",
    "On Jul", "On Aug", "On Sep", "On Oct", "On Nov", "On Dec",
    "From: ", "\nFrom:",                            # forwarded message header
    "IMPORTANT: The contents of this email",        # Steve's signature footer
    "Click Here To Schedule",                       # Steve-specific signature element
    "CONFIDENTIALITY", "Confidentiality Notice",    # Common legal footer
)


def _strip_reply_signature(text: str) -> str:
    """Cut at the first signature/quote-chain marker. Falls through to a
    soft cap (800 chars) if no marker is found so the preview stays readable."""
    if not text:
        return ""
    earliest = len(text)
    for marker in _SIGNATURE_DELIMITERS:
        idx = text.find(marker)
        if 0 <= idx < earliest:
            earliest = idx
    cut = text[:earliest].rstrip()
    # Soft cap — long single-paragraph replies still get trimmed
    if len(cut) > 800:
        cut = cut[:800].rstrip()
    return cut


# Phone formats we'll extract. Loose enough to catch (702) 555-1234, 702-555-1234,
# 702.555.1234, 7025551234, +1-702-555-1234, etc.
_PHONE_BARE_RE = re.compile(r"(?:\+?1[-.\s]?)?\(?(\d{3})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})")
# Phone with explicit "M:" / "Mobile:" / "Cell:" / "C:" label preceding it
_MOBILE_LABEL_RE = re.compile(
    r"\b(?:M|Mobile|Cell|C)[:.]?\s*(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})",
    re.IGNORECASE,
)
# Phone with "O:" / "Office:" / "Direct:" / "Tel:" / "T:"
_OFFICE_LABEL_RE = re.compile(
    r"\b(?:O|Office|Direct|D|Tel|T|Phone|P)[:.]?\s*(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})",
    re.IGNORECASE,
)
_LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:www\.)?linkedin\.com/(?:in|pub)/[\w%\-_.]+",
    re.IGNORECASE,
)


def _normalize_phone(s: str) -> str:
    """Reuse the e164 normalizer from twilio_voice for consistency."""
    try:
        from app.services.twilio_voice import normalize_phone_e164
        return normalize_phone_e164(s) or ""
    except Exception:
        # Bare-bones fallback — strip non-digits, prepend +1 if 10 digits
        digits = re.sub(r"\D", "", s or "")
        if len(digits) == 10:
            return f"+1{digits}"
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"
        return ""


def parse_signature_for_enrichment(body_text: str) -> dict:
    """Mine an inbound email body (signature + main text) for high-value
    contact enrichment fields. Returns a dict with optional keys:

      mobile_phone — phone explicitly labeled as mobile/cell
      office_phone — phone explicitly labeled as office/direct
      any_phone    — first phone found if no labels matched
      linkedin_url — first LinkedIn profile URL

    Conservative: returns None for fields we couldn't confidently detect.
    Caller decides whether to auto-apply (only when the contact's
    corresponding field is empty)."""
    out: dict = {}
    if not body_text:
        return out
    s = body_text

    m = _MOBILE_LABEL_RE.search(s)
    if m:
        out["mobile_phone"] = _normalize_phone(m.group(1))

    m = _OFFICE_LABEL_RE.search(s)
    if m:
        out["office_phone"] = _normalize_phone(m.group(1))

    if not (out.get("mobile_phone") or out.get("office_phone")):
        m = _PHONE_BARE_RE.search(s)
        if m:
            out["any_phone"] = _normalize_phone(m.group(0))

    li = _LINKEDIN_RE.search(s)
    if li:
        url = li.group(0)
        if not url.lower().startswith("http"):
            url = "https://" + url
        out["linkedin_url"] = url.rstrip("/")

    # Drop empty values that fell through normalization
    return {k: v for k, v in out.items() if v}


_DISPLAY_NAME_RE = re.compile(r"^([^<]+)<")
def _extract_display_name(from_raw: str) -> str:
    m = _DISPLAY_NAME_RE.match(from_raw or "")
    return (m.group(1).strip().strip('"') if m else "")


_AUTO_RESPONSE_FROM_PATTERNS = (
    "noreply@", "no-reply@", "donotreply@", "do-not-reply@",
    "mailer-daemon@", "postmaster@", "bounces@", "auto-reply@",
)
_AUTO_RESPONSE_SUBJECT_HINTS = (
    "out of office", "auto-reply", "automatic reply", "vacation",
    "delivery status", "undeliverable", "delivery failure", "returned mail",
    "mail delivery", "could not be delivered",
)
def _looks_like_auto_response(from_addr: str, subject: str, body: str) -> bool:
    """Best-effort detection of auto-responders / bounces / OOO messages.
    These shouldn't auto-pause sequences — the human didn't actually engage."""
    fa = (from_addr or "").lower()
    sub = (subject or "").lower()
    if any(p in fa for p in _AUTO_RESPONSE_FROM_PATTERNS):
        return True
    if any(h in sub for h in _AUTO_RESPONSE_SUBJECT_HINTS):
        return True
    # X-Auto-Response or Precedence headers would be more reliable but
    # Resend's payload doesn't expose them; the from/subject heuristic
    # catches ~95% of these in practice.
    return False


async def _forward_to_bdr(
    sender_user: User,
    prospect_email: str,
    prospect_name: str,
    subject: str,
    body_text: str,
    body_html: str,
    contact: Optional[Contact],
    company: Optional[Company],
    inbound_id: Optional[str] = None,
    db=None,
):
    """Forward the prospect's reply to the BDR's actual inbox.

    Reply-To is set to the prospect's real email address so when the BDR
    hits Reply in their inbox tool (Missive / Gmail / etc.), the response
    goes directly to the prospect — bypassing our token route. That means
    follow-up replies in this thread happen entirely outside our system,
    which is the right behavior: we capture the FACT of the reply + open
    the thread, the human handles the conversation.

    The forwarded email looks like a normal direct email from the prospect's
    email client perspective — no 'Fwd:' prefix, no chrome — so it threads
    cleanly in the BDR's inbox alongside the original outbound."""
    bdr_inbox = sender_user.email  # their @bymp.com address → Missive/Gmail/whatever
    # Forward must NOT look like the rep emailing themselves. Sending From the
    # rep's own name on the sending subdomain TO the rep's primary domain
    # (rodrigo@go.bymp.com → rodrigo@bymp.com) is a display-name self-spoof
    # that Gmail/Workspace reliably files to spam — which is exactly why reps
    # weren't seeing replies in their inbox (Rodrigo's feedback). Instead send
    # From the PROSPECT's display name on a neutral "replies@" mailbox, with
    # Reply-To = the prospect, so it threads as a reply from them and the rep's
    # Reply goes straight to the prospect.
    prospect_display = (prospect_name or prospect_email or "Prospect").strip()

    # Body — prefer the original HTML if present (preserves formatting).
    # Add a small contextual header so the BDR sees CRM context inline.
    contact_label = (contact.full_name if contact else "") or prospect_name or prospect_email
    company_label = (company.name if company else "")
    crm_url = f"{settings.public_url.rstrip('/')}/?company_id={company.id}" if company else settings.public_url
    context_header_html = (
        f'<div style="background:#f4faf4;border-left:3px solid #1B5E20;padding:8px 12px;margin-bottom:14px;'
        f'font-size:12px;color:#555;font-family:-apple-system,BlinkMacSystemFont,sans-serif">'
        f'📥 <strong>Reply from {contact_label}</strong>'
        f'{f" at {company_label}" if company_label else ""} · captured by Prospector · '
        f'<a href="{crm_url}">open in CRM</a>'
        f'</div>'
    )
    forwarded_html = context_header_html + (body_html if body_html else f"<pre>{(body_text or '').replace('<', '&lt;')}</pre>")

    result = await send_email(
        to_email=bdr_inbox,
        subject=subject or "(no subject)",
        body=forwarded_html,
        from_name=prospect_display,
        from_firstname="replies",  # → replies@<tenant send domain>; not the rep's own address
        reply_to_email=prospect_email,  # BDR replies → goes direct to prospect, not back through us
        company_id=(company.id if company else 0),
        contact_id=(contact.id if contact else 0),
        email_id=0,  # not tied to a specific GeneratedEmail row
        signature_html="",  # no auto-signature on forwards — already a real conversation
        unsubscribe_token=None,  # this isn't outreach, no compliance footer needed
    )
    if db is not None and result.get("success") and inbound_id:
        from app.services.credit_meter import meter, make_idem_key
        await meter(
            db, action_type="email_send",
            idempotency_key=make_idem_key("email_send", "forward", inbound_id),
            user_id=sender_user.id,
            action_ref=f"forward_inbound:{inbound_id}",
        )
