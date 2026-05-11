"""Add feedback and pending_deletions tables."""
import sqlite3, pathlib

DB = pathlib.Path(__file__).resolve().parent.parent / "leads.db"

FEEDBACK_TABLE = """
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    category VARCHAR(40) NOT NULL DEFAULT 'feedback',
    message TEXT NOT NULL,
    page VARCHAR(80),
    resolved BOOLEAN NOT NULL DEFAULT 0,
    admin_notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

PENDING_DELETIONS_TABLE = """
CREATE TABLE IF NOT EXISTS pending_deletions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_by INTEGER NOT NULL REFERENCES users(id),
    entity_type VARCHAR(20) NOT NULL,
    entity_id INTEGER NOT NULL,
    entity_name VARCHAR(255),
    reason VARCHAR(255),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

def migrate():
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "feedback" not in tables:
        cur.execute(FEEDBACK_TABLE)
        print("[migrate_feedback_deletions] created feedback table")
    if "pending_deletions" not in tables:
        cur.execute(PENDING_DELETIONS_TABLE)
        print("[migrate_feedback_deletions] created pending_deletions table")
    con.commit()
    con.close()
    print("[migrate_feedback_deletions] done")

if __name__ == "__main__":
    migrate()
