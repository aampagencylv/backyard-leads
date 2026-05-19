"""Schema audit — does every Column declared on a SQLAlchemy model
exist in the live database?

This catches the "shipped a feature, forgot the migration" class of bug
that produced the lost_reason 500s. Two ways to use it:

  - CLI: `python -m scripts.audit_schema` (human) or `--json` (machine).
    Exits 1 on drift so it can gate CI.
  - Library: `await compare_schema(engine)` from app code.
    init_db calls this after migrations and pushes any drift into the
    middleware error ring so the admin dashboard's System Errors panel
    surfaces it without anyone reading journalctl.
"""
from __future__ import annotations
import asyncio
import json
import sys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from app.models import Base


async def compare_schema(engine: AsyncEngine) -> dict:
    """Compare declared models vs live DB. Returns a report dict.

    Does NOT dispose the engine — callers own its lifecycle. Safe to
    call from app startup (uses a borrowed connection).
    """
    declared: dict[str, set[str]] = {}
    for table_name, table in Base.metadata.tables.items():
        declared[table_name] = {col.name for col in table.columns}

    async with engine.connect() as conn:
        try:
            await conn.execute(text("SELECT 1 FROM information_schema.tables LIMIT 1"))
            dialect = "postgres"
        except Exception:
            dialect = "sqlite"

        live: dict[str, set[str]] = {}
        if dialect == "postgres":
            rows = (await conn.execute(text("""
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
            """))).all()
            for tn, cn in rows:
                live.setdefault(tn, set()).add(cn)
        else:
            tables = [r[0] for r in (await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ))).all()]
            for tn in tables:
                cols = [r[1] for r in (await conn.execute(text(
                    f"PRAGMA table_info({tn})"
                ))).all()]
                live[tn] = set(cols)

    missing_tables: list[str] = []
    missing_columns: list[dict] = []
    extra_tables: list[str] = []
    extra_columns: list[dict] = []

    for tname, cols in declared.items():
        if tname not in live:
            missing_tables.append(tname)
            continue
        for col in cols - live[tname]:
            missing_columns.append({"table": tname, "column": col})
        for col in live[tname] - cols:
            extra_columns.append({"table": tname, "column": col})

    for tname in set(live) - set(declared):
        if tname.startswith("pg_") or tname in {"alembic_version", "spatial_ref_sys"}:
            continue
        extra_tables.append(tname)

    return {
        "dialect": dialect,
        "tables_declared": len(declared),
        "tables_live": len(live),
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "extra_tables": sorted(extra_tables),
        "extra_columns": extra_columns,
        "clean": not missing_tables and not missing_columns,
    }


async def main(json_out: bool) -> int:
    from app.database import engine
    report = await compare_schema(engine)

    if json_out:
        print(json.dumps(report, indent=2))
    else:
        print(f"Dialect: {report['dialect']}")
        print(f"Declared tables: {report['tables_declared']}")
        print(f"Live tables:     {report['tables_live']}\n")
        if report["missing_tables"]:
            print("  MISSING TABLES (declared in models.py, not in DB):")
            for t in report["missing_tables"]:
                print(f"    - {t}")
        if report["missing_columns"]:
            print("  MISSING COLUMNS (declared in models.py, not in DB):")
            for mc in report["missing_columns"]:
                print(f"    - {mc['table']}.{mc['column']}")
        if report["extra_tables"]:
            print("\n  Extra tables in DB (informational — not a bug):")
            for t in report["extra_tables"]:
                print(f"    - {t}")
        if report["extra_columns"]:
            print("\n  Extra columns in DB (informational — old migrations or manual ALTERs):")
            for ec in report["extra_columns"][:20]:
                print(f"    - {ec['table']}.{ec['column']}")
            if len(report["extra_columns"]) > 20:
                print(f"    ... and {len(report['extra_columns']) - 20} more")
        print()
        if report["clean"]:
            print("✓ Schema clean — every declared column exists in the DB.")
        else:
            print("✗ Schema drift detected. Add a migration for each missing column / table.")

    await engine.dispose()
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    json_out = "--json" in sys.argv
    sys.exit(asyncio.run(main(json_out)))
