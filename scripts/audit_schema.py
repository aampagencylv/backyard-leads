"""Schema audit — does every Column declared on a SQLAlchemy model
exist in the live database?

This catches the "shipped a feature, forgot the migration" class of bug
that produced the lost_reason 500s. Run after every deploy. Run from CI.
Run before promoting a branch that touches models.py.

Exit code 0 = clean. Exit code 1 = drift detected (so it can gate a CI
job). Pass --json for machine-readable output.

Usage:
  python -m scripts.audit_schema           # human readable
  python -m scripts.audit_schema --json    # JSON report
"""
from __future__ import annotations
import asyncio
import json
import sys
from sqlalchemy import text
from app.database import engine
from app.models import Base


async def main(json_out: bool):
    declared: dict[str, set[str]] = {}
    for table_name, table in Base.metadata.tables.items():
        declared[table_name] = {col.name for col in table.columns}

    async with engine.connect() as conn:
        # Detect Postgres vs SQLite by checking for information_schema
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
    extra_tables: list[str] = []  # in DB but not declared — usually fine
    extra_columns: list[dict] = []  # in DB but not declared — usually fine

    for tname, cols in declared.items():
        if tname not in live:
            missing_tables.append(tname)
            continue
        for col in cols - live[tname]:
            missing_columns.append({"table": tname, "column": col})
        for col in live[tname] - cols:
            extra_columns.append({"table": tname, "column": col})

    for tname in set(live) - set(declared):
        # Internal Postgres / migration bookkeeping tables we don't care about
        if tname.startswith("pg_") or tname in {"alembic_version", "spatial_ref_sys"}:
            continue
        extra_tables.append(tname)

    report = {
        "dialect": dialect,
        "tables_declared": len(declared),
        "tables_live": len(live),
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "extra_tables": sorted(extra_tables),
        "extra_columns": extra_columns,
        "clean": not missing_tables and not missing_columns,
    }

    if json_out:
        print(json.dumps(report, indent=2))
    else:
        print(f"Dialect: {dialect}")
        print(f"Declared tables: {report['tables_declared']}")
        print(f"Live tables:     {report['tables_live']}\n")
        if missing_tables:
            print(f"  MISSING TABLES (declared in models.py, not in DB):")
            for t in missing_tables:
                print(f"    - {t}")
        if missing_columns:
            print(f"  MISSING COLUMNS (declared in models.py, not in DB):")
            for mc in missing_columns:
                print(f"    - {mc['table']}.{mc['column']}")
        if extra_tables:
            print(f"\n  Extra tables in DB (informational — not a bug):")
            for t in extra_tables:
                print(f"    - {t}")
        if extra_columns:
            print(f"\n  Extra columns in DB (informational — old migrations or manual ALTERs):")
            for ec in extra_columns[:20]:
                print(f"    - {ec['table']}.{ec['column']}")
            if len(extra_columns) > 20:
                print(f"    ... and {len(extra_columns) - 20} more")
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
