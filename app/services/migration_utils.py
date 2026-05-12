"""
Cross-dialect helpers for the idempotent ALTER-TABLE migrations.

Every `scripts/migrate_*.py` follows the same pattern: check whether a
column already exists, and ADD it if not. Under SQLite that used
`PRAGMA table_info(X)`; under Postgres we use the standard
`information_schema.columns` view.

Centralizing the check here means each migration script can be written
once and works against either backend — important during the SQLite →
Supabase Postgres cutover and beyond (the same helper supports tests
running on in-memory SQLite if we ever add them).
"""
from __future__ import annotations
from sqlalchemy import text


async def column_exists(conn, table: str, column: str) -> bool:
    """True if `table.column` is already defined. Works on SQLite + Postgres."""
    dialect = conn.engine.url.get_backend_name() if hasattr(conn, "engine") else conn.dialect.name
    if dialect == "sqlite":
        rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
        return column in {r[1] for r in rows}
    # Postgres + any other ANSI-SQL information_schema-aware backend
    row = (await conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c LIMIT 1"
        ),
        {"t": table, "c": column},
    )).first()
    return row is not None


async def table_exists(conn, table: str) -> bool:
    """True if `table` is already created. Works on SQLite + Postgres."""
    dialect = conn.engine.url.get_backend_name() if hasattr(conn, "engine") else conn.dialect.name
    if dialect == "sqlite":
        row = (await conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t LIMIT 1"),
            {"t": table},
        )).first()
        return row is not None
    row = (await conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = :t LIMIT 1"
        ),
        {"t": table},
    )).first()
    return row is not None
