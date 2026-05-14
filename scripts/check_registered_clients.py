"""Check which Twilio client identities are currently registered (online)."""
import asyncio
import httpx
from app.database import async_session
from app.runtime_config import get_twilio_credentials

TWILIO_BASE = "https://api.twilio.com/2010-04-01"

async def main():
    async with async_session() as db:
        creds = await get_twilio_credentials(db)
        auth = (creds.account_sid, creds.auth_token)

        # Twilio doesn't have a direct "list registered clients" API.
        # But we can check the Calls API for recent attempts and their status.
        # The real way to verify is through the Twilio console → Voice → Clients
        # or by checking if the Access Token grant is properly configured.

        print("=== Twilio Voice SDK Configuration Check ===")
        print(f"  Account SID: {creds.account_sid[:10]}...")
        print(f"  API Key SID: {creds.api_key_sid or 'NOT SET'}")
        print(f"  API Key Secret: {'***' if creds.api_key_secret else 'NOT SET'}")
        print(f"  TwiML App SID: {creds.twiml_app_sid or 'NOT SET'}")

        if not creds.api_key_sid or not creds.api_key_secret:
            print("\n  ⚠️  API Key not configured — Voice SDK tokens cannot be generated!")
            return

        if not creds.twiml_app_sid:
            print("\n  ⚠️  TwiML App SID not configured!")
            return

        # Verify the TwiML App exists and check its voice_url
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{TWILIO_BASE}/Accounts/{creds.account_sid}/Applications/{creds.twiml_app_sid}.json",
                auth=auth
            )
            if r.status_code == 200:
                app = r.json()
                print(f"\n  TwiML App: {app.get('friendly_name')}")
                print(f"  Voice URL: {app.get('voice_url')}")

                # The key thing: for inbound client calls to work, the
                # access token must include a VoiceGrant with:
                # - outgoing_application_sid = twiml_app_sid
                # - incoming_allow = True
                print(f"\n  VoiceGrant config (from code):")
                print(f"    outgoing_application_sid: {creds.twiml_app_sid}")
                print(f"    incoming_allow: True")

            # Try to generate a token and decode it to verify the grant
            from app.services.twilio_voice import generate_access_token
            try:
                token = generate_access_token(creds, identity="test_check", ttl_seconds=60)
                import base64, json
                # JWT has 3 parts separated by dots
                parts = token.split('.')
                if len(parts) == 3:
                    # Decode the payload (part 2)
                    payload = parts[1] + '=' * (4 - len(parts[1]) % 4)  # pad
                    decoded = json.loads(base64.b64decode(payload))
                    grants = decoded.get('grants', {})
                    voice = grants.get('voice', {})
                    print(f"\n  Token grants.voice:")
                    print(f"    outgoing.application_sid: {voice.get('outgoing', {}).get('application_sid', 'MISSING')}")
                    print(f"    incoming.allow: {voice.get('incoming', {}).get('allow', 'MISSING')}")
                    if not voice.get('incoming', {}).get('allow'):
                        print(f"\n  ⚠️  CRITICAL: incoming.allow is not True — inbound calls CANNOT reach this client!")
                    else:
                        print(f"\n  ✓ Token looks correct for inbound calls")
                print(f"\n  Token identity for test: test_check")
            except Exception as e:
                print(f"\n  ERROR generating test token: {e}")

asyncio.run(main())
