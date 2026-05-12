"""
One-shot patcher — rewrites every migration script that uses SQLite's
PRAGMA table_info() to instead call the cross-dialect column_exists()
helper. Run once; safe to re-run (idempotent — looks for the pattern,
skips already-patched files).

Pattern transformation:

  BEFORE:
    cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(<T>)"))).fetchall()}
    if "<C>" not in cols:
        ...

  AFTER:
    from app.services.migration_utils import column_exists
    if not await column_exists(conn, "<T>", "<C>"):
        ...

The for-loop variant:
  BEFORE:
    cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(<T>)"))).fetchall()}
    for name, ddl in COLUMNS:
        if name not in cols:
            await conn.execute(text(f"ALTER TABLE <T> ADD COLUMN {name} {ddl}"))

  AFTER:
    from app.services.migration_utils import column_exists
    for name, ddl in COLUMNS:
        if not await column_exists(conn, "<T>", name):
            await conn.execute(text(f"ALTER TABLE <T> ADD COLUMN {name} {ddl}"))
"""
from __future__ import annotations
import re
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
IMPORT_LINE = "from app.services.migration_utils import column_exists\n"


def patch_one(path: Path) -> bool:
    text = path.read_text()
    if "PRAGMA table_info" not in text:
        return False

    original = text

    # Drop the `cols = {...PRAGMA...}` prefetch line. We're going to
    # use column_exists() at each check site instead.
    text = re.sub(
        r'^\s*cols\s*=\s*\{r\[1\] for r in \(await conn\.execute\(text\(["\']PRAGMA table_info\(([^)]+)\)["\']\)\)\)\.fetchall\(\)\}.*\n',
        "",
        text,
        flags=re.MULTILINE,
    )

    # Replace `if "foo" not in cols:` → `if not await column_exists(conn, "<TABLE>", "foo"):`
    # We need to know the table name. The patcher uses the table name from the
    # nearest PRAGMA we found above; if there are multiple tables in the same
    # file we need to be smarter. Walk the original text and build a table list.
    table_for_block: list[tuple[int, str]] = []
    for m in re.finditer(r'PRAGMA table_info\(([^)]+)\)', original):
        table_for_block.append((m.start(), m.group(1).strip()))

    # If exactly one table referenced, simple replace.
    if len({t for _, t in table_for_block}) == 1 and table_for_block:
        only_table = table_for_block[0][1]
        text = re.sub(
            r'if\s+["\']([^"\']+)["\']\s+not in cols\s*:',
            lambda m: f'if not await column_exists(conn, "{only_table}", "{m.group(1)}"):',
            text,
        )
        text = re.sub(
            r'if\s+name\s+not in cols\s*:',
            f'if not await column_exists(conn, "{only_table}", name):',
            text,
        )
    else:
        # Multi-table file — patch manually
        print(f"  ! {path.name} references multiple tables: {sorted({t for _, t in table_for_block})} — patch by hand")
        return False

    # Add the import. Place it after the existing `from sqlalchemy import text`
    # so flake/lint stays happy.
    if IMPORT_LINE not in text:
        text = re.sub(
            r'(from sqlalchemy import [^\n]+\n)',
            r'\1' + IMPORT_LINE,
            text,
            count=1,
        )

    if text != original:
        path.write_text(text)
        return True
    return False


def main() -> None:
    patched = 0
    skipped = 0
    for script in sorted(SCRIPTS_DIR.glob("migrate_*.py")):
        try:
            if patch_one(script):
                patched += 1
                print(f"  + {script.name}")
            else:
                skipped += 1
        except Exception as e:
            print(f"  ! {script.name} failed: {e}")
    print(f"\nDone. {patched} patched, {skipped} skipped (already patched / no PRAGMA).")


if __name__ == "__main__":
    main()
