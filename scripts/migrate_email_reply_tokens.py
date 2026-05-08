"""
Add generated_emails.reply_token for token-based reply catching.

Every outgoing email gets a unique random token. Reply-To address is
`r-<token>@inbound.bymp.com`. When the prospect replies, the inbound
webhook (Resend Inbound) extracts the token, looks up the GeneratedEmail
row, logs the reply, auto-pauses the sequence, and forwards the message
to the BDR's actual inbox.

Tokens never expire — a months-late reply still threads correctly.
Existing pre-token emails won't get replies tracked this way (their
Reply-To still points at the user's main-domain address → Missive),
which is fine; only NEW outgoing email gets the token treatment.
Idempotent.
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(generated_emails)"))).fetchall()}
        if "reply_token" not in cols:
            await conn.execute(text("ALTER TABLE generated_emails ADD COLUMN reply_token VARCHAR(40)"))
            print("+ added generated_emails.reply_token")
        # Unique index — fast lookup AND prevents collisions if two random tokens
        # ever clash (vanishingly unlikely with 20 url-safe bytes but free protection).
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_generated_emails_reply_token "
            "ON generated_emails(reply_token) WHERE reply_token IS NOT NULL"
        ))
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
