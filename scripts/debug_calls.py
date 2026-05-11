"""Debug: check recent call activities and their recording status."""
import sqlite3, pathlib

DB = pathlib.Path(__file__).resolve().parent.parent / "leads.db"

con = sqlite3.connect(str(DB))
cur = con.cursor()

print("=== Last 10 call activities ===")
rows = cur.execute("""
    SELECT a.id, a.activity_type, a.call_duration_seconds,
           CASE WHEN a.recording_url IS NOT NULL AND a.recording_url != '' THEN 'YES' ELSE 'NO' END as has_recording,
           a.recording_url,
           CASE WHEN a.transcript IS NOT NULL AND a.transcript != '' THEN 'YES' ELSE 'NO' END as has_transcript,
           a.call_outcome, a.twilio_call_sid, a.created_at,
           c.name as company_name
    FROM activities a
    LEFT JOIN companies c ON a.company_id = c.id
    WHERE a.activity_type IN ('call', 'voicemail')
    ORDER BY a.created_at DESC LIMIT 10
""").fetchall()

for r in rows:
    print(f"  ID={r[0]} type={r[1]} dur={r[2]}s rec={r[3]} transcript={r[5]} outcome={r[6]}")
    print(f"    sid={r[7]} company={r[9]}")
    print(f"    created={r[8]}")
    if r[4]:
        print(f"    recording_url={r[4][:80]}")
    print()

print(f"=== Total calls: {len(rows)} ===")

# Check if any recording webhooks have hit recently
print("\n=== Activities with recording URLs ===")
recs = cur.execute("SELECT id, recording_url, created_at FROM activities WHERE recording_url IS NOT NULL AND recording_url != '' ORDER BY created_at DESC LIMIT 5").fetchall()
if recs:
    for r in recs:
        print(f"  ID={r[0]} url={r[1][:80]} at={r[2]}")
else:
    print("  NONE — no recordings have ever been saved")

con.close()
