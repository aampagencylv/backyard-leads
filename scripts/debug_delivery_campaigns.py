"""Debug: check email delivery status and campaign health."""
import sqlite3, pathlib

DB = pathlib.Path(__file__).resolve().parent.parent / "leads.db"
con = sqlite3.connect(str(DB))
cur = con.cursor()

print("=== Email status overview ===")
total = cur.execute("SELECT COUNT(*) FROM generated_emails").fetchone()[0]
sent = cur.execute("SELECT COUNT(*) FROM generated_emails WHERE is_sent = 1").fetchone()[0]
pending = cur.execute("SELECT COUNT(*) FROM generated_emails WHERE is_sent = 0 AND paused_at IS NULL AND skipped_at IS NULL").fetchone()[0]
paused = cur.execute("SELECT COUNT(*) FROM generated_emails WHERE paused_at IS NOT NULL").fetchone()[0]
skipped = cur.execute("SELECT COUNT(*) FROM generated_emails WHERE skipped_at IS NOT NULL").fetchone()[0]
print(f"  Total: {total}  Sent: {sent}  Pending: {pending}  Paused: {paused}  Skipped: {skipped}")

print("\n=== Last 10 emails (any status) ===")
rows = cur.execute("""
    SELECT ge.id, ge.step_type, ge.is_sent, ge.sent_at, ge.paused_at, ge.skipped_at, ge.skip_reason,
           ge.scheduled_send_at, ge.auto_execute, c.email as to_email, co.name as company,
           substr(ge.subject, 1, 50)
    FROM generated_emails ge
    LEFT JOIN contacts c ON ge.contact_id = c.id
    LEFT JOIN companies co ON ge.company_id = co.id
    ORDER BY ge.id DESC LIMIT 10
""").fetchall()
for r in rows:
    status = "SENT" if r[2] else ("PAUSED" if r[4] else ("SKIPPED" if r[5] else "PENDING"))
    print(f"  #{r[0]} {r[1]} {status} auto={r[8]} to={r[9]} co={r[10]}")
    print(f"    scheduled={r[7]} subject={r[11]}")
    if r[6]: print(f"    skip_reason={r[6]}")

print("\n=== Resend webhook activity events ===")
try:
    for ev_type in ('email_sent', 'email_delivered', 'email_opened', 'email_clicked', 'email_bounced', 'email_replied'):
        cnt = cur.execute("SELECT COUNT(*) FROM activities WHERE activity_type = ?", (ev_type,)).fetchone()[0]
        if cnt > 0:
            print(f"  {ev_type}: {cnt}")
    # Check if any sent activities at all
    sent_acts = cur.execute("SELECT COUNT(*) FROM activities WHERE activity_type = 'email_sent'").fetchone()[0]
    if sent_acts == 0:
        print("  NO email_sent activities found — emails may not be going out at all")
except Exception as e:
    print(f"  Error: {e}")

print("\n=== Sequence engine errors (recent log) ===")
try:
    errors = cur.execute("""
        SELECT COUNT(*) FROM activities WHERE activity_type LIKE '%error%' OR activity_type LIKE '%fail%'
    """).fetchone()[0]
    print(f"  Error activities: {errors}")
except:
    pass

print("\n=== Campaigns ===")
try:
    cols = [r[1] for r in cur.execute("PRAGMA table_info(campaigns)").fetchall()]
    campaigns = cur.execute("SELECT * FROM campaigns ORDER BY rowid DESC LIMIT 5").fetchall()
    if campaigns:
        for c in campaigns:
            data = dict(zip(cols, c))
            print(f"\n  Campaign #{data.get('id')}: {data.get('name', '?')}")
            print(f"    status={data.get('status')} mode={data.get('mode')}")
            print(f"    prospects_today={data.get('prospects_today')} max_per_day={data.get('max_prospects_per_day')}")
            print(f"    loc_idx={data.get('current_location_index')}")
            print(f"    created={data.get('created_at')}")
            # Targets
            try:
                tcount = cur.execute("SELECT COUNT(*) FROM campaign_targets WHERE campaign_id = ?", (data['id'],)).fetchone()[0]
                print(f"    targets: {tcount}")
            except:
                pass
    else:
        print("  No campaigns found")
except Exception as e:
    print(f"  Error reading campaigns: {e}")

print("\n=== Campaign runs (last 10) ===")
try:
    runs = cur.execute("""
        SELECT id, campaign_id, status, companies_found, companies_qualified,
               sequences_created, substr(error_message, 1, 100), created_at
        FROM campaign_runs ORDER BY id DESC LIMIT 10
    """).fetchall()
    if runs:
        for r in runs:
            print(f"  Run #{r[0]} campaign={r[1]} status={r[2]} found={r[3]} qualified={r[4]} seqs={r[5]} at={r[7]}")
            if r[6]: print(f"    error: {r[6]}")
    else:
        print("  No runs found")
except Exception as e:
    print(f"  Error: {e}")

con.close()
