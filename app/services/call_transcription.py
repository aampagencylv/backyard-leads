"""
Call transcription pipeline.

Triggered async when Twilio's recording-complete webhook fires.

Flow:
  Twilio recording URL → Deepgram (telephony-grade ASR + speaker diarization)
                       → Claude Sonnet (structured takeaways + coaching tips)
                       → save transcript + summary onto the Activity row.

Deepgram is a drop-in for OpenAI Whisper but tuned for phone audio: better
accuracy on compressed/noisy lines, native speaker diarization (no need
for dual-channel post-processing), and 5-10× faster turnaround. Cost:
$0.0043/min for Nova-2 vs Whisper's $0.006/min.

Notable design choices:
  * Recording URL is passed to Deepgram's `?url=...` param so we never
    download the audio onto our server. Deepgram fetches it with our
    Twilio basic-auth credentials embedded in the URL.
  * Diarization output is collapsed into a clean "Rep / Prospect"
    transcript by mapping speaker 0 to whoever spoke first (the rep,
    since outbound calls have the rep on the dialer first).
  * Talk-to-listen ratio is computed from word counts per speaker and
    fed to Claude as input — drives the coaching suggestions.
"""
from __future__ import annotations
import asyncio
import json
import logging
from typing import Optional
from urllib.parse import quote, urlparse, urlunparse

import httpx
import anthropic
from sqlalchemy import select

from app.database import async_session
from app.models import Activity, Contact, Company
from app.runtime_config import get_twilio_credentials, get_deepgram_api_key
from app.config import settings


log = logging.getLogger(__name__)


DEEPGRAM_LISTEN_URL = "https://api.deepgram.com/v1/listen"


async def transcribe_and_summarize_in_background(activity_id: int) -> None:
    """
    Entry point — call from a webhook with `asyncio.create_task(...)`.
    Runs the full pipeline; logs errors but never raises (since the caller
    already returned to Twilio).
    """
    try:
        await _run_pipeline(activity_id)
    except Exception as e:
        log.exception("transcription pipeline failed for activity %s: %s", activity_id, e)


async def _run_pipeline(activity_id: int) -> None:
    async with async_session() as db:
        act = (await db.execute(
            select(Activity).where(Activity.id == activity_id)
        )).scalar_one_or_none()
        if not act:
            log.warning("activity %s not found for transcription", activity_id)
            return
        if not act.recording_url:
            log.warning("activity %s has no recording_url", activity_id)
            return
        if act.transcript:
            log.info("activity %s already transcribed; skipping", activity_id)
            return

        deepgram_key = await get_deepgram_api_key(db)
        if not deepgram_key:
            log.info("no Deepgram key configured; skipping transcription for activity %s", activity_id)
            return

        twilio_creds = await get_twilio_credentials(db)
        # The Twilio recording URL needs basic auth; embed creds so Deepgram can fetch it
        signed_recording_url = _embed_basic_auth(act.recording_url, twilio_creds.account_sid, twilio_creds.auth_token)

        # 1. Deepgram transcription
        try:
            transcript_text, diarized_segments, talk_ratio = await _transcribe_with_deepgram(
                signed_recording_url, deepgram_key
            )
        except Exception as e:
            # A 404/REMOTE_CONTENT_ERROR means Twilio no longer serves the
            # recording (purged, or the URL isn't live yet when we fire
            # right after the recording webhook). Expected occasionally —
            # log as warning so Sentry doesn't page; everything else stays
            # ERROR via log.exception.
            msg = str(e)
            if "REMOTE_CONTENT_ERROR" in msg or "404" in msg:
                log.warning("Deepgram could not fetch recording for activity %s: %s",
                            activity_id, msg[:200])
            else:
                log.exception("Deepgram transcription failed for activity %s", activity_id)
            return

        # Persist the transcript + structured diarization first — even
        # if Claude summarization fails later. The dashboard waveform +
        # talk-ratio panel both read these JSON blobs directly.
        import json as _json
        act.transcript = transcript_text
        # Normalize the talk_ratio dict to include prospect_pct too (the
        # legacy helper only set rep_pct). UI reads both.
        try:
            tr = dict(talk_ratio or {})
            rep_pct = float(tr.get("rep_pct") or 0.0)
            tr["prospect_pct"] = round(max(0.0, 100.0 - rep_pct), 1)
            act.talk_ratio_json = _json.dumps(tr)
        except Exception:
            act.talk_ratio_json = None
        try:
            act.diarized_segments_json = _json.dumps(diarized_segments or [])
        except Exception:
            act.diarized_segments_json = None
        await db.commit()

        # 2. Pull contact + company context for the prompt
        contact = None
        company = None
        if act.contact_id:
            contact = (await db.execute(select(Contact).where(Contact.id == act.contact_id))).scalar_one_or_none()
        if act.company_id:
            company = (await db.execute(select(Company).where(Company.id == act.company_id))).scalar_one_or_none()

        # 3. Claude summary
        if not settings.anthropic_api_key:
            log.info("no Anthropic key; skipping summary for activity %s", activity_id)
            return
        try:
            summary = await _summarize_with_claude(
                transcript_text, diarized_segments, talk_ratio,
                contact=contact, company=company,
            )
        except Exception:
            log.exception("Claude summary failed for activity %s", activity_id)
            return

        act.call_summary = summary
        await db.commit()
        log.info("transcription pipeline complete for activity %s", activity_id)


# ============================================================
# Deepgram
# ============================================================

async def _transcribe_with_deepgram(
    recording_url: str,
    api_key: str,
    timeout_seconds: int = 120,
) -> tuple[str, list[dict], dict]:
    """
    POST the recording URL to Deepgram's listen endpoint.
    Returns:
      (joined_transcript_with_speaker_labels, raw_segments, talk_ratio_dict)
    """
    # multichannel=true is the gold standard for Twilio call recordings:
    # Twilio is set to record-from-answer-dual, so each leg lands on its
    # own channel (channel 0 = the rep/caller, channel 1 = the prospect/
    # callee). Deepgram returns a separate transcript per channel, which
    # is far more accurate than single-channel diarize=true (no risk of
    # mislabeling speakers based on voice similarity).
    params = {
        "model": "nova-2",
        "multichannel": "true",
        "smart_format": "true",
        "punctuate": "true",
        "language": "en",
        "paragraphs": "true",
        "utterances": "true",
    }
    # Twilio recording URLs require auth — download the audio first,
    # then send raw bytes to Deepgram instead of a URL reference.
    from app.runtime_config import get_twilio_credentials
    from app.database import async_session
    audio_bytes = None
    if "api.twilio.com" in recording_url or "twilio" in recording_url.lower():
        async with async_session() as _db:
            _creds = await get_twilio_credentials(_db)
        async with httpx.AsyncClient(timeout=60) as client:
            audio_r = await client.get(recording_url, auth=(_creds.account_sid, _creds.auth_token))
            if audio_r.status_code == 200:
                audio_bytes = audio_r.content

    headers = {"Authorization": f"Token {api_key}"}

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        if audio_bytes:
            headers["Content-Type"] = "audio/mpeg"
            r = await client.post(DEEPGRAM_LISTEN_URL, params=params, headers=headers, content=audio_bytes)
        else:
            headers["Content-Type"] = "application/json"
            r = await client.post(DEEPGRAM_LISTEN_URL, params=params, headers=headers, json={"url": recording_url})
    if r.status_code != 200:
        raise RuntimeError(f"Deepgram {r.status_code}: {r.text[:300]}")

    data = r.json()
    # Drill into the result shape. With multichannel=true, results.channels
    # is a list — channel 0 = rep (caller leg), channel 1 = prospect (callee
    # leg). Each channel has its own alternatives[].words[] with absolute
    # timestamps, so we can interleave by time to build a unified transcript.
    channels = (data.get("results", {}).get("channels", []) or [])
    if not channels:
        return "", [], {"rep": 0, "prospect": 0, "rep_pct": 0, "prospect_pct": 0}

    # Collect words from every channel, tagging each with its channel index
    # (= speaker). Sort by start time so the interleaved transcript reads
    # in conversation order.
    all_words: list[dict] = []
    for ch_idx, ch in enumerate(channels):
        alt = (ch.get("alternatives", []) or [])
        if not alt:
            continue
        for w in (alt[0].get("words", []) or []):
            all_words.append({
                "speaker": ch_idx,  # channel index doubles as speaker label
                "start": float(w.get("start", 0.0)),
                "end": float(w.get("end", 0.0)),
                "text": w.get("punctuated_word") or w.get("word", ""),
            })
    all_words.sort(key=lambda w: w["start"])

    # If we somehow got nothing (silent recording), bail with empties.
    if not all_words:
        return "", [], {"rep": 0, "prospect": 0, "rep_pct": 0, "prospect_pct": 0}

    # Group consecutive same-speaker words into utterances. A gap of
    # >0.8s within the same speaker also starts a new segment so we
    # don't merge two separate sentences.
    segments: list[dict] = []
    current = None
    GAP_BREAK = 0.8
    for w in all_words:
        if current is None or current["speaker"] != w["speaker"] or (w["start"] - current["end"]) > GAP_BREAK:
            if current:
                segments.append(current)
            current = {"speaker": w["speaker"], "start": w["start"], "end": w["end"], "text": w["text"]}
        else:
            current["end"] = w["end"]
            current["text"] += " " + w["text"]
    if current:
        segments.append(current)

    # Build a pretty transcript with speaker labels.
    def speaker_label(sp: int) -> str:
        return "Rep" if sp == 0 else "Prospect"

    pretty = "\n\n".join(
        f"**{speaker_label(s['speaker'])}:** {s['text'].strip()}"
        for s in segments if s.get("text", "").strip()
    )

    # Talk ratio — count words per channel.
    word_counts: dict[int, int] = {}
    for w in all_words:
        sp = int(w["speaker"])
        word_counts[sp] = word_counts.get(sp, 0) + 1

    # Detect "single-speaker" recordings — voicemail, dropped call, one
    # side muted, etc. When only one channel produced any words, label
    # the recording as such so the UI can render an accurate hint
    # rather than implying the other party did all (or none) of the
    # talking. We still surface the word counts; just add a flag.
    speakers_with_words = [sp for sp, c in word_counts.items() if c > 0]
    single_speaker = len(speakers_with_words) <= 1
    rep_words = word_counts.get(0, 0)
    prospect_words = sum(c for sp, c in word_counts.items() if sp != 0)
    total = max(rep_words + prospect_words, 1)
    talk_ratio = {
        "rep_words": rep_words,
        "prospect_words": prospect_words,
        "rep_pct": round(rep_words * 100 / total, 1),
        "single_speaker": single_speaker,
    }

    return pretty, segments, talk_ratio


# ============================================================
# Claude — structured takeaways + coaching suggestions
# ============================================================

CALL_SUMMARY_SYSTEM_PROMPT = """You are a sales-call review specialist for Backyard Marketing Pros (BMP),
a marketing agency that sells digital marketing services to home-services
businesses (pool builders, landscapers, outdoor-kitchen builders, deck/fence/
patio contractors, etc.) primarily in Las Vegas, Phoenix, Houston, and Miami.

The team uses BMP's CRM to make outbound cold calls to prospects discovered
via Google Maps. Your job is to review a call transcript and produce a
concise, actionable summary that helps the rep follow up effectively AND
helps them improve their next call.

Format your response in Markdown with these sections, in this exact order:

### Summary
2-3 sentences capturing what the call was about and how it ended.

### Outcome
- **Status**: connected / left voicemail / no answer / declined / callback requested / other
- **Next step**: what was agreed (if anything)

### Key takeaways
- 3-5 specific bullet points the rep should remember about this prospect

### Objections raised
- List specific objections (or "None raised")

### Action items for the rep
- Things the rep committed to doing (or "None")

### Coaching suggestions
- 1-3 specific, kind, practical suggestions for the rep's next call.
  Focus on listening, asking better questions, handling objections, or
  positioning. If the rep talked too much (>60% of words), gently flag it.

Be concise. Bullets, not paragraphs. Use the prospect's actual words
when quoting. Don't invent details that aren't in the transcript."""


async def _summarize_with_claude(
    transcript: str,
    segments: list[dict],
    talk_ratio: dict,
    contact: Optional[Contact] = None,
    company: Optional[Company] = None,
) -> str:
    """Run the transcript through Claude Sonnet and return Markdown takeaways."""
    contact_label = (
        contact.full_name if contact and contact.full_name else
        (contact.email if contact and contact.email else "the prospect")
    )
    company_label = company.name if company else "their company"
    industry = (company.business_type or "home services") if company else "home services"

    user_prompt = f"""Call between BMP rep and {contact_label} at {company_label} ({industry}).

**Talk ratio**: rep spoke {talk_ratio.get('rep_pct', 0)}% of words ({talk_ratio.get('rep_words', 0)} rep / {talk_ratio.get('prospect_words', 0)} prospect)

**Transcript** (Rep = your team member, Prospect = {contact_label}):

{transcript}
"""

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    # Use the shared model constant, not a hardcoded dated snapshot.
    # claude-sonnet-4-20250514 was retired and started 404ing, so every
    # call summary silently failed (transcript still saved, summary did
    # not). MODEL_BALANCED tracks the current Sonnet.
    from app.services.ai_client import MODEL_BALANCED
    response = await client.messages.create(
        model=MODEL_BALANCED,
        max_tokens=1200,
        system=CALL_SUMMARY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    from app.services.credit_meter import meter_standalone as _meter_ai
    await _meter_ai(action_type="ai_summary", action_ref="call_transcript_summary",
                    raw_cost_override_usd=0.012,  # 1200 max_tokens output ~ $0.01-0.015
                    metadata={"max_tokens": 1200, "kind": "call_summary"})
    return response.content[0].text


# ============================================================
# Helpers
# ============================================================

def _embed_basic_auth(url: str, sid: str, token: str) -> str:
    """Embed Twilio basic auth in a URL so Deepgram can fetch the recording.
    Twilio recording URLs require Account SID + Auth Token.
    """
    if not (sid and token):
        return url
    parsed = urlparse(url)
    netloc = f"{quote(sid, safe='')}:{quote(token, safe='')}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))
