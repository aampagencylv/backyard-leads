"""
Public unsubscribe endpoint — no auth required, signed by per-contact token.
Marks the contact as unsubscribed and pauses any active sequence.
CAN-SPAM requires this to be honored within 10 business days; we do it instantly.
"""
from __future__ import annotations
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.tenancy import get_tenant_db
from app.models import Contact, GeneratedEmail, Activity

router = APIRouter(tags=["unsubscribe"])


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(
    t: str = Query(..., min_length=10, description="Per-contact unsubscribe token"),
    db: AsyncSession = Depends(get_tenant_db),
):
    from app.runtime_config import get_org_brand
    brand_name = (await get_org_brand(db)).get("company_name", "")

    contact = (await db.execute(
        select(Contact).where(Contact.unsubscribe_token == t)
    )).scalar_one_or_none()

    if not contact:
        return HTMLResponse(_page("Unsubscribe link is no longer valid", "If you continue to receive emails, reply to one and we'll remove you manually.", brand_name), status_code=404)

    if not contact.unsubscribed_at:
        contact.unsubscribed_at = datetime.now(timezone.utc)
        # Pause any pending emails for this contact (legacy table)
        pending = (await db.execute(
            select(GeneratedEmail).where(
                GeneratedEmail.contact_id == contact.id,
                GeneratedEmail.is_sent == False,
                GeneratedEmail.paused_at.is_(None),
            )
        )).scalars().all()
        now = datetime.now(timezone.utc)
        for e in pending:
            e.paused_at = now

        # POST-CUTOVER: also terminate the engagement engine engagement
        # for this contact AND add their email to email_suppressions so
        # EmailChannel.pre_dispatch_guards blocks any future engine send.
        # WITHOUT these two steps the legacy half pauses, but the new
        # engine keeps firing — actively continuing outreach to a
        # prospect who explicitly opted out (CAN-SPAM violation).
        engine_actions_canceled = 0
        try:
            from app.engagement_engine.lifecycle import terminate_engagement
            engine_actions_canceled = await terminate_engagement(
                db, contact.id, reason="unsubscribed_via_link",
                transition_by="system",
            )
        except Exception:
            pass

        # Add to suppression list so any future re-enrollment doesn't
        # accidentally re-target the unsubscribed address.
        if contact.email:
            try:
                from sqlalchemy import text as _sa_text
                await db.execute(_sa_text("""
                    INSERT INTO email_suppressions (
                        tenant_id, recipient_email, reason, source,
                        is_currently_active
                    )
                    VALUES (:t, :r, 'unsubscribe', 'unsubscribe_link', TRUE)
                    ON CONFLICT (tenant_id, recipient_email)
                      WHERE is_currently_active = TRUE
                      DO NOTHING
                """), {"t": contact.tenant_id, "r": contact.email})
            except Exception:
                pass

        db.add(Activity(
            company_id=contact.company_id,
            contact_id=contact.id,
            activity_type="unsubscribed",
            content=(
                f"Unsubscribed via email link; "
                f"{len(pending)} legacy email(s) paused, "
                f"{engine_actions_canceled} engine action(s) canceled, "
                f"email added to suppression list"
            ),
        ))
        await db.commit()

    _from_clause = f" from {brand_name}" if brand_name else ""
    return HTMLResponse(_page(
        "You've been unsubscribed",
        f"We've removed you from this email list. You won't receive any further messages{_from_clause} related to this outreach. Thanks for letting us know.",
        brand_name,
    ))


def _page(title: str, body: str, brand_name: str = "") -> str:
    _title_suffix = f" — {brand_name}" if brand_name else ""
    _footer = (f'<p style="margin-top:24px;font-size:12px;color:#888">— '
               f'<span class="brand">{brand_name}</span></p>') if brand_name else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}{_title_suffix}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; background: #f7f9f7; margin: 0; padding: 40px 20px; color: #333; }}
.card {{ max-width: 520px; margin: 60px auto; background: white; border-radius: 12px; padding: 32px; box-shadow: 0 4px 20px rgba(0,0,0,0.06); }}
h1 {{ color: #1e4634; margin: 0 0 12px; font-size: 22px; }}
p {{ line-height: 1.6; font-size: 14px; }}
.brand {{ color: #FF723F; font-weight: 600; }}
</style>
</head>
<body>
  <div class="card">
    <h1>{title}</h1>
    <p>{body}</p>
    {_footer}
  </div>
</body>
</html>"""
