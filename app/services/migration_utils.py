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


# ----------------------------------------------------------------------
# schema_migrations ledger — fast-path startup
# ----------------------------------------------------------------------
#
# Every migration the init_db chain runs gets recorded here by name.
# On every subsequent startup, the chain checks the ledger first and
# skips anything already applied. That collapses ~60s of cold-start
# migration thrashing (44 × 15 × 100ms RTT to Supabase) into a single
# `SELECT name FROM schema_migrations` (~150ms).

async def ensure_schema_migrations_table(conn) -> None:
    """Create the ledger table if it doesn't exist. Idempotent."""
    dialect = conn.engine.url.get_backend_name() if hasattr(conn, "engine") else conn.dialect.name
    if dialect == "sqlite":
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))
    else:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name VARCHAR(255) PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))


async def applied_migrations(conn) -> set[str]:
    """Return the set of names already in the ledger. One query, no loop."""
    rows = (await conn.execute(text("SELECT name FROM schema_migrations"))).fetchall()
    return {r[0] for r in rows}


async def mark_applied(conn, name: str) -> None:
    """Record a migration as applied. Caller decides what counts as success."""
    dialect = conn.engine.url.get_backend_name() if hasattr(conn, "engine") else conn.dialect.name
    if dialect == "sqlite":
        await conn.execute(
            text("INSERT OR IGNORE INTO schema_migrations (name) VALUES (:n)"),
            {"n": name},
        )
    else:
        await conn.execute(
            text(
                "INSERT INTO schema_migrations (name) VALUES (:n) "
                "ON CONFLICT (name) DO NOTHING"
            ),
            {"n": name},
        )
