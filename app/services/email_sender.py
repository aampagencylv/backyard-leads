"""
Email Sending Service via Resend
Handles sending emails, tracking opens, and processing webhooks.
"""
from __future__ import annotations
from typing import Optional
import httpx
from datetime import datetime, timezone
from app.config import settings


def _compliance_footer(unsubscribe_token: str | None) -> str:
    """CAN-SPAM-compliant footer: physical postal address + unsubscribe link."""
    address = settings.bmp_postal_address
    unsub_link = ""
    if unsubscribe_token:
        url = f"{settings.public_url.rstrip('/')}/unsubscribe?t={unsubscribe_token}"
        unsub_link = f'<br><a href="{url}" style="color:#888;text-decoration:underline">Unsubscribe</a>'
    return f"""
    <div style="margin-top:20px;padding-top:12px;border-top:1px solid #e5e7eb;font-size:11px;color:#888;font-family:Arial,sans-serif;">
        {address}{unsub_link}
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
    footer = _compliance_footer(unsubscribe_token)
    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; color: #333; line-height: 1.6;">
        {body_html}
        {sig_block}
        {footer}
    </div>
    """

    payload = {
        "from": from_address,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
        "reply_to": reply_to_email,
        "headers": {
            "X-Company-ID": str(company_id),
            "X-Contact-ID": str(contact_id),
            "X-Email-ID": str(email_id),
        },
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
