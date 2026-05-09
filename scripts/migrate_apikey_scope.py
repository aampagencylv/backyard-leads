"""
Add `scope` column to api_keys for MCP v2a write actions.

Existing keys default to 'read' scope (safe — they keep working for
the search/get/summarize tools they could already call). New keys
created via the Integrations UI can opt into 'write' scope to unlock
mutation tools (add_note, enroll_in_sequence, book_meeting, etc.).

Idempotent. Auto-runs on startup via init_db().
"""
from __future__ import annotations
import asyncio
from sqlalchemy import text
from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(api_keys)"))).fetchall()}
        if "scope" not in cols:
            await conn.execute(text(
                "ALTER TABLE api_keys ADD COLUMN scope TEXT NOT NULL DEFAULT 'read'"
            ))
            print("+ added api_keys.scope")
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
