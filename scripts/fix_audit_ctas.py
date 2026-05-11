"""One-time fix: replace 'Let's Talk' with 'Schedule A Discovery Call' in existing audit reports."""
import sqlite3
import pathlib

DB = pathlib.Path(__file__).resolve().parent.parent / "leads.db"

def fix():
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    rows = cur.execute("SELECT id, html_content FROM audit_reports").fetchall()
    fixed = 0
    for rid, html in rows:
        if not html:
            continue
        original = html
        # Fix "Let's Talk" button text
        html = html.replace(">Let's Talk<", ">📅 Schedule A Discovery Call<")
        html = html.replace(">Let&#x27;s Talk<", ">📅 Schedule A Discovery Call<")
        if html != original:
            cur.execute("UPDATE audit_reports SET html_content = ? WHERE id = ?", (html, rid))
            fixed += 1
            print(f"  Fixed report {rid}")
    con.commit()
    con.close()
    print(f"Done — fixed {fixed} reports")

if __name__ == "__main__":
    fix()
