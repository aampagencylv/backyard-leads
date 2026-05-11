"""One-time: strip UTM query params from existing company website URLs."""
import sqlite3, pathlib

DB = pathlib.Path(__file__).resolve().parent.parent / "leads.db"

def clean():
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    rows = cur.execute("SELECT id, website FROM companies WHERE website LIKE '%utm_%' OR website LIKE '%gclid%' OR website LIKE '%fbclid%'").fetchall()
    for cid, url in rows:
        clean_url = url.split("?")[0].rstrip("/")
        cur.execute("UPDATE companies SET website = ? WHERE id = ?", (clean_url, cid))
        print(f"  {cid}: {url[:80]} -> {clean_url}")
    con.commit()
    con.close()
    print(f"Cleaned {len(rows)} URLs")

if __name__ == "__main__":
    clean()
