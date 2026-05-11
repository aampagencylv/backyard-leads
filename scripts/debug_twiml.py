"""Debug: generate the outbound TwiML and check if recording attributes are present."""
from app.services.twilio_voice import build_outbound_twiml

twiml = build_outbound_twiml(
    to_number="+17025551234",
    caller_id="+17025559999",
    record_calls=True,
    recording_status_callback="https://prospector.backyardmarketingpros.com/api/twilio/voice/recording",
    consent_disclosure=True,
)
print("=== Generated TwiML ===")
print(twiml)
print()

if "record" in twiml.lower():
    print("OK: 'record' attribute found in TwiML")
else:
    print("PROBLEM: no 'record' attribute in TwiML — calls won't be recorded!")

if "recordingStatusCallback" in twiml or "recordingstatuscallback" in twiml.lower():
    print("OK: recordingStatusCallback found in TwiML")
else:
    print("PROBLEM: no recordingStatusCallback — even if recorded, no webhook!")
