"""
Email Sending Service via Resend
Handles sending emails, tracking opens, and processing webhooks.
"""
from __future__ import annotations
from typing import Optional
import httpx
from datetime import datetime, timezone
from app.config import settings


async def send_email(
    to_email: str,
    subject: str,
    body: str,
    from_name: str,
    from_firstname: str,
    reply_to_email: str,
    lead_id: int,
    email_id: int,
    signature_html: str = "",
) -> dict:
    """
    Send an email via Resend API.
    Returns dict with resend message ID and status.
    """
    from_address = f"{from_name} <{from_firstname}@{settings.send_domain}>"

    # Convert plain text body to simple HTML
    body_html = body.replace("\n", "<br>")
    sig_block = f'<div style="margin-top:24px">{signature_html}</div>' if signature_html else ""
    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; color: #333; line-height: 1.6;">
        {body_html}
        {sig_block}
    </div>
    """

    payload = {
        "from": from_address,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
        "reply_to": reply_to_email,
        "headers": {
            "X-Lead-ID": str(lead_id),
            "X-Email-ID": str(email_id),
        },
        "tags": [
            {"name": "lead_id", "value": str(lead_id)},
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
            return {
                "success": True,
                "resend_id": data.get("id"),
                "message": "Email sent successfully",
            }
        else:
            return {
                "success": False,
                "error": response.text,
                "status_code": response.status_code,
            }


def get_sender_info(first_name: str, full_name: str) -> dict:
    """
    Derive sender email addresses from user's first name (preferred) or full name.
    e.g. first_name="Steve" -> steve@go.backyardmarketingpros.com
    """
    fn = (first_name or "").strip().lower()
    if not fn and full_name:
        fn = full_name.strip().split()[0].lower()
    return {
        "from_name": full_name,
        "from_firstname": fn,
        "from_email": f"{fn}@{settings.send_domain}",
        "reply_to": f"{fn}@{settings.reply_domain}",
    }
