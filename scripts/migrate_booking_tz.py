"""Add prospect_timezone column to bookings table."""
import sqlite3, pathlib

DB = pathlib.Path(__file__).resolve().parent.parent / "leads.db"

def migrate():
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(bookings)").fetchall()}
    if "prospect_timezone" not in cols:
        cur.execute("ALTER TABLE bookings ADD COLUMN prospect_timezone VARCHAR(80)")
        con.commit()
        print("[migrate_booking_tz] added prospect_timezone")
    else:
        print("[migrate_booking_tz] already exists")
    con.close()

if __name__ == "__main__":
    migrate()
