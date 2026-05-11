"""Backfill recording URLs from Twilio API for calls that have recordings
but our webhook never received them."""
import asyncio, httpx
from app.database import async_session
from app.runtime_config import get_twilio_credentials
from app.models import Activity
from sqlalchemy import select

async def main():
    async with async_session() as db:
        creds = await get_twilio_credentials(db)

        # Get all recordings from Twilio
        url = f"https://api.twilio.com/2010-04-01/Accounts/{creds.account_sid}/Recordings.json"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params={"PageSize": 100}, auth=(creds.account_sid, creds.auth_token))

        recordings = r.json().get("recordings", [])
        print(f"Found {len(recordings)} recordings in Twilio")

        for rec in recordings:
            call_sid = rec.get("call_sid")
            rec_sid = rec.get("sid")
            duration = rec.get("duration")
            recording_url = f"https://api.twilio.com/2010-04-01/Accounts/{creds.account_sid}/Recordings/{rec_sid}.mp3"

            # Find the matching activity
            act = (await db.execute(
                select(Activity).where(Activity.twilio_call_sid == call_sid)
            )).scalar_one_or_none()

            if act:
                if not act.recording_url:
                    act.recording_url = recording_url
                    print(f"  Backfilled activity {act.id} (call_sid={call_sid}) with recording {rec_sid} ({duration}s)")
                else:
                    print(f"  Activity {act.id} already has recording URL, skipping")
            else:
                print(f"  No activity found for call_sid={call_sid}, skipping")

        await db.commit()

        # Now trigger transcription for any activities that have recordings but no transcript
        activities_to_transcribe = (await db.execute(
            select(Activity).where(
                Activity.recording_url.isnot(None),
                Activity.recording_url != "",
                Activity.transcript.is_(None),
            )
        )).scalars().all()

        if activities_to_transcribe:
            print(f"\nTriggering transcription for {len(activities_to_transcribe)} recordings...")
            from app.services.call_transcription import transcribe_and_summarize_in_background
            for act in activities_to_transcribe:
                print(f"  Transcribing activity {act.id}...")
                try:
                    await transcribe_and_summarize_in_background(act.id)
                    print(f"  Done: activity {act.id}")
                except Exception as e:
                    print(f"  Failed: {e}")

    print("\nBackfill complete")

asyncio.run(main())
