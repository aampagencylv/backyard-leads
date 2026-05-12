"""Debug: check email delivery status and campaign health."""
import sqlite3, pathlib

DB = pathlib.Path(__file__).resolve().parent.parent / "leads.db"
con = sqlite3.connect(str(DB))
cur = con.cursor()

print("=== Last 15 sent emails ===")
rows = cur.execute("""
    SELECT ge.id, ge.subject, ge.sent_at, c.email as to_email,
           ge.step_type, ge.sent_by_user_id
    FROM generated_emails ge
    JOIN contacts c ON ge.contact_id = c.id
    WHERE ge.is_sent = 1
    ORDER BY ge.sent_at DESC LIMIT 15
""").fetchall()
for r in rows:
    print(f"  ID={r[0]} to={r[3]} sent={r[5] or '?'} at={r[4]}")
    print(f"    subject: {(r[1] or '')[:60]}")
print(f"Total sent: {len(rows)}")

print("\n=== Email event tracking (opens/clicks/bounces) ===")
events = cur.execute("""
    SELECT event_type, COUNT(*) FROM email_events GROUP BY event_type
""").fetchall()
if events:
    for ev, cnt in events:
        print(f"  {ev}: {cnt}")
else:
    # Check if table exists
    tbl = cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='email_events'").fetchone()
    if tbl:
        print("  No events recorded yet")
    else:
        print("  email_events table does not exist")

print("\n=== Resend webhook status (recent) ===")
try:
    webhooks = cur.execute("""
        SELECT activity_type, COUNT(*) FROM activities
        WHERE activity_type IN ('email_delivered', 'email_opened', 'email_clicked', 'email_bounced')
        GROUP BY activity_type
    """).fetchall()
    if webhooks:
        for at, cnt in webhooks:
            print(f"  {at}: {cnt}")
    else:
        print("  No delivery events tracked in activities")
except Exception as e:
    print(f"  Error: {e}")

print("\n=== Campaigns ===")
# Get column names first
cols = [r[1] for r in cur.execute("PRAGMA table_info(campaigns)").fetchall()]
print(f"  Columns: {', '.join(cols[:15])}")
campaigns = cur.execute("SELECT * FROM campaigns ORDER BY rowid DESC LIMIT 5").fetchall()
if campaigns:
    for c in campaigns:
        data = dict(zip(cols, c))
        print(f"\n  Campaign #{data.get('id')}: {data.get('name', '?')}")
        print(f"    status={data.get('status')} mode={data.get('mode')}")
        print(f"    prospects_today={data.get('prospects_today')} max_per_day={data.get('max_prospects_per_day')}")
        print(f"    location_idx={data.get('current_location_index')}")
        print(f"    created={data.get('created_at')}")
        # Check for targets
        try:
            targets = cur.execute("SELECT COUNT(*) FROM campaign_targets WHERE campaign_id = ?", (data['id'],)).fetchone()
            print(f"    targets: {targets[0] if targets else 0}")
        except:
            pass
else:
    print("  No campaigns found")

print("\n=== Campaign runs (last 10) ===")
try:
    runs = cur.execute("""
        SELECT id, campaign_id, status, companies_found, companies_qualified,
               sequences_created, error_message, created_at
        FROM campaign_runs ORDER BY id DESC LIMIT 10
    """).fetchall()
    for r in runs:
        print(f"  Run #{r[0]} campaign={r[1]} status={r[2]} found={r[3]} qualified={r[4]} sequences={r[5]}")
        if r[6]:
            print(f"    error: {r[6][:100]}")
        print(f"    at={r[7]}")
except Exception as e:
    print(f"  Error: {e}")

con.close()
