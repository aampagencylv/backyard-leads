"""
Email Sending Service via Resend
Handles sending emails, tracking opens, and processing webhooks.
"""
from __future__ import annotations
from typing import Optional
import httpx
from datetime import datetime, timezone
from app.config import settings


def _compliance_footer() -> str:
    """
    Minimal compliance footer. Postal address only (CAN-SPAM requirement).
    No visible unsubscribe link — Gmail/Outlook handle that via the
    List-Unsubscribe HTTP header (set in the Resend payload), which surfaces
    as a native button at the top of the email instead of footer copy.
    Visible footer "click to unsubscribe" links trigger Gmail Promotions
    classification and hurt inbox placement.
    """
    return f"""
    <div style="margin-top:20px;font-size:11px;color:#999;font-family:Arial,sans-serif;">
        {settings.bmp_postal_address}
    </div>
    """


async def send_email(
    to_email: str,
    subject: str,
    body: str,
    from_name: str,
    from_firstname: str,
    reply_to_email: str,
    company_id: int,
    contact_id: int,
    email_id: int,
    signature_html: str = "",
    unsubscribe_token: str | None = None,
) -> dict:
    from_address = f"{from_name} <{from_firstname}@{settings.send_domain}>"

    body_html = body.replace("\n", "<br>")
    sig_block = f'<div style="margin-top:24px">{signature_html}</div>' if signature_html else ""
    footer = _compliance_footer()
    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; color: #333; line-height: 1.6;">
        {body_html}
        {sig_block}
        {footer}
    </div>
    """

    headers = {
        "X-Company-ID": str(company_id),
        "X-Contact-ID": str(contact_id),
        "X-Email-ID": str(email_id),
    }
    # List-Unsubscribe headers — invisible to recipient, but Gmail/Outlook
    # use them to render a native unsubscribe button at the top of the email
    # AND treat the sender as more legitimate (better inbox placement).
    if unsubscribe_token:
        unsub_url = f"{settings.public_url.rstrip('/')}/unsubscribe?t={unsubscribe_token}"
        headers["List-Unsubscribe"] = f"<{unsub_url}>"
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    # Wrap the Reply-To with the sender's display name so the recipient's email
    # client shows "Steven Edwards" instead of the raw r-<token>@go.bymp.com
    # address when they hit Reply. The token is still in the address for routing,
    # but it's tucked behind the human name. Most clients (Gmail, Apple Mail,
    # Outlook, etc.) honor the display name in Reply-To headers.
    reply_to_value = f"{from_name} <{reply_to_email}>" if from_name else reply_to_email

    # Plain-text alternative — HTML-only emails are a soft spam signal at
    # Gmail/Outlook, and a clean text part also produces sane quoted-reply
    # output. Derive from the same HTML so the two versions never diverge.
    try:
        from app.services.html_to_text import html_to_plain_text
        text_body = html_to_plain_text(html_body)
    except Exception:
        text_body = body  # last-ditch fallback to the raw input
    payload = {
        "from": from_address,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
        "text": text_body,
        "reply_to": reply_to_value,
        "headers": headers,
        "tags": [
            {"name": "company_id", "value": str(company_id)},
            {"name": "contact_id", "value": str(contact_id)},
            {"name": "email_id", "value": str(email_id)},
        ],
    }

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if response.status_code in (200, 201):
            data = response.json()
            return {"success": True, "resend_id": data.get("id"), "message": "Email sent successfully"}
        return {"success": False, "error": response.text, "status_code": response.status_code}


def get_sender_info(first_name: str, full_name: str) -> dict:
    """Derive sender email from first name (preferred) or full name."""
    fn = (first_name or "").strip().lower()
    if not fn and full_name:
        fn = full_name.strip().split()[0].lower()
    return {
        "from_name": full_name,
        "from_firstname": fn,
        "from_email": f"{fn}@{settings.send_domain}",
        "reply_to": f"{fn}@{settings.reply_domain}",
    }


def generate_reply_token() -> str:
    """32-char lowercase-hex token for the Reply-To routing address.

    Email local-parts get normalized to lowercase by most mail providers
    (Resend / SES / Postmark all do this). Mixed-case tokens like
    `secrets.token_urlsafe(20)` round-trip through the wire as lowercase
    and our DB lookup misses. Hex is always lowercase → no normalization
    issue. 16 random bytes = 128 bits of entropy, plenty for a routing key."""
    import secrets
    return secrets.token_hex(16)


def reply_to_for_token(token: str) -> str:
    """Build the Reply-To address for a given token.

    `r-` prefix is what the inbound webhook parser splits on to extract
    the token. Keeps the local-part visually identifiable as a system
    address so a savvy prospect can see what's going on if they look,
    but doesn't reveal anything sensitive."""
    return f"r-{token}@{settings.inbound_reply_domain}"
