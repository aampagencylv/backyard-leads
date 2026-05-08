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
            log.exception("Deepgram transcription failed for activity %s", activity_id)
            return

        # Persist the transcript first — even if Claude summarization fails later
        act.transcript = transcript_text
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
    params = {
        "model": "nova-2",
        "diarize": "true",
        "smart_format": "true",
        "punctuate": "true",
        "language": "en",
        "paragraphs": "true",
        "utterances": "true",
    }
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": "application/json",
    }
    body = {"url": recording_url}

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        r = await client.post(DEEPGRAM_LISTEN_URL, params=params, headers=headers, json=body)
    if r.status_code != 200:
        raise RuntimeError(f"Deepgram {r.status_code}: {r.text[:300]}")

    data = r.json()
    # Drill into the result shape
    channels = (data.get("results", {}).get("channels", []) or [])
    if not channels:
        return "", [], {"rep": 0, "prospect": 0, "rep_pct": 0}

    alt = (channels[0].get("alternatives", []) or [])
    if not alt:
        return "", [], {"rep": 0, "prospect": 0, "rep_pct": 0}

    paragraph_block = alt[0].get("paragraphs", {}).get("transcript", "")
    raw_words = alt[0].get("words", []) or []

    # Build speaker-labeled segments by walking utterances if present, else words
    utterances = data.get("results", {}).get("utterances", []) or []
    segments: list[dict] = []
    if utterances:
        for u in utterances:
            segments.append({
                "speaker": int(u.get("speaker", 0)),
                "start": float(u.get("start", 0.0)),
                "end": float(u.get("end", 0.0)),
                "text": u.get("transcript", ""),
            })
    elif raw_words:
        # Fallback — group consecutive words by speaker
        current = None
        for w in raw_words:
            sp = int(w.get("speaker", 0))
            if current is None or current["speaker"] != sp:
                if current:
                    segments.append(current)
                current = {"speaker": sp, "start": float(w.get("start", 0)),
                           "end": float(w.get("end", 0)), "text": w.get("punctuated_word") or w.get("word", "")}
            else:
                current["end"] = float(w.get("end", current["end"]))
                current["text"] += " " + (w.get("punctuated_word") or w.get("word", ""))
        if current:
            segments.append(current)

    # Map speaker 0 → "Rep" (whoever spoke first on outbound dial),
    # speaker 1+ → "Prospect"
    def speaker_label(sp: int) -> str:
        return "Rep" if sp == 0 else "Prospect"

    pretty = "\n\n".join(
        f"**{speaker_label(s['speaker'])}:** {s['text'].strip()}"
        for s in segments if s.get("text", "").strip()
    ) or paragraph_block

    # Talk ratio (rough word-count ratio per speaker)
    word_counts: dict[int, int] = {}
    for w in raw_words:
        sp = int(w.get("speaker", 0))
        word_counts[sp] = word_counts.get(sp, 0) + 1
    rep_words = word_counts.get(0, 0)
    prospect_words = sum(c for sp, c in word_counts.items() if sp != 0)
    total = max(rep_words + prospect_words, 1)
    talk_ratio = {
        "rep_words": rep_words,
        "prospect_words": prospect_words,
        "rep_pct": round(rep_words * 100 / total, 1),
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
    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
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
