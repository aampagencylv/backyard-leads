"""LEGACY (SQLite-only) — add voicemail_greeting_url column to users.
Kept for historical reference; column lives in the SQLAlchemy model so
a fresh Postgres install gets it via create_all. Do not run on Postgres."""
import sqlite3, pathlib

DB = pathlib.Path(__file__).resolve().parent.parent / "leads.db"

def migrate():
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()}
    if "voicemail_greeting_url" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN voicemail_greeting_url VARCHAR(500)")
        con.commit()
        print("[migrate_voicemail_greeting] added voicemail_greeting_url")
    else:
        print("[migrate_voicemail_greeting] already exists")
    con.close()

if __name__ == "__main__":
    migrate()
