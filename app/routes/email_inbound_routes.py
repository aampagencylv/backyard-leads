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


def _verify_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """Resend webhook signature uses Svix-style format (same vendor for
    both outbound + inbound webhooks). Defense-in-depth: if the secret
    isn't configured (early dev), we accept everything; once it's set,
    we reject anything that doesn't carry a valid HMAC."""
    if not secret or not signature_header:
        return not bool(secret)  # accept when secret unset, reject when set+missing-sig
    try:
        # Svix format: 'v1,base64sig v1,base64sig2'
        # We compute HMAC-SHA256 of raw_body with the secret and compare.
        # Defensive: tolerate the 'v1,' prefix and pick the first sig pair.
        for token in signature_header.split():
            if "," in token:
                _, sig = token.split(",", 1)
                expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
                import base64
                if hmac.compare_digest(base64.b64encode(expected).decode(), sig):
                    return True
        return False
    except Exception:
        return False


@router.post("/inbound")
async def email_inbound(request: Request):
    """Resend Inbound webhook receiver. Public — no auth. Optionally
    HMAC-verified via settings.resend_webhook_secret."""
    raw = await request.body()
    sig_header = request.headers.get("svix-signature") or request.headers.get("resend-signature") or ""
    secret = await _resolve_resend_webhook_secret()
    if not _verify_signature(raw, sig_header, secret):
        return JSONResponse({"ok": False, "error": "bad signature"}, status_code=401)

    try:
        payload = json.loads(raw or b"{}")
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    event_type = payload.get("type") or ""
    if event_type and event_type != "email.received":
        # Not a received email — could be a delivery / bounce event we don't care about here.
        return {"ok": True, "ignored": event_type}

    data = payload.get("data") or payload  # tolerant — direct payload OR wrapped
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
    body_preview = body_for_log[:400] + ("…" if len(body_for_log) > 400 else "")

    # Auto-responder / bounce detection — common patterns in From or Subject.
    # We still log these but DON'T auto-pause (the human didn't actually engage).
    is_auto_response = _looks_like_auto_response(from_addr, subject, body_for_log)

    async with async_session() as db:
        ge = (await db.execute(
            select(GeneratedEmail).where(GeneratedEmail.reply_token == token)
        )).scalar_one_or_none()
        if not ge:
            log.warning(f"[inbound] Token {token[:8]}… not found in DB — silent drop. From: {bare_from}")
            return {"ok": True, "ignored": "unknown_token"}

        contact = (await db.execute(select(Contact).where(Contact.id == ge.contact_id))).scalar_one_or_none()
        company = (await db.execute(select(Company).where(Company.id == ge.company_id))).scalar_one_or_none()

        # Log Activity to the contact's timeline
        activity_type = "email_auto_response" if is_auto_response else "email_replied"
        prefix = "[Auto-response]" if is_auto_response else "[Reply]"
        db.add(Activity(
            company_id=ge.company_id,
            contact_id=ge.contact_id,
            activity_type=activity_type,
            content=f"{prefix} {subject or '(no subject)'} — {body_preview or '(empty body)'}",
            metadata_json=json.dumps({
                "from": bare_from,
                "from_raw": from_addr,
                "subject": subject,
                "preview": body_preview,
                "email_id": ge.id,
                "is_auto_response": is_auto_response,
            }),
        ))

        # Auto-pause + status bump only on REAL replies
        if not is_auto_response:
            from app.services.sequence_engine import pause_sequence
            try:
                await pause_sequence(
                    db, ge.contact_id,
                    reason=f"prospect replied to '{ge.subject}'",
                    sequence_label=(ge.sequence_label or "main"),
                )
            except Exception as e:
                log.exception(f"[inbound] pause_sequence failed: {e}")
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
                    )
                except Exception as e:
                    log.exception(f"[inbound] forward to BDR failed: {e}")

    return {"ok": True, "token_matched": token[:8] + "…", "auto_response": is_auto_response}


# ============================================================
# Helpers
# ============================================================

_HTML_TAG_RE = re.compile(r"<[^>]+>")
def _strip_html(s: str) -> str:
    return _HTML_TAG_RE.sub("", s or "").strip()


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
    from app.services.email_sender import get_sender_info
    sender = get_sender_info(sender_user.first_name, sender_user.full_name)
    bdr_inbox = sender_user.email  # their @bymp.com address → Missive/Gmail/whatever

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

    await send_email(
        to_email=bdr_inbox,
        subject=subject or "(no subject)",
        body=forwarded_html,
        from_name=sender["from_name"],
        from_firstname=sender["from_firstname"],
        reply_to_email=prospect_email,  # BDR replies → goes direct to prospect, not back through us
        company_id=(company.id if company else 0),
        contact_id=(contact.id if contact else 0),
        email_id=0,  # not tied to a specific GeneratedEmail row
        signature_html="",  # no auto-signature on forwards — already a real conversation
        unsubscribe_token=None,  # this isn't outreach, no compliance footer needed
    )
