"""
Reply sentiment classifier.

Takes the body of an inbound prospect reply and returns one of six
sentiment buckets + a one-line gist. Drives the colored badge on the
timeline + (later) auto-routing rules.

Cost: ~$0.001-0.003 per classify. Metered as ai_reply_classify.

Buckets (kept tight on purpose so the UI can color-code):
  - interested      — they want to talk / send pricing / next step
  - objection       — pushback that needs a human response
  - out_of_office   — auto-OOO / vacation
  - wrong_person    — "I don't handle this, talk to X"
  - unsubscribe     — explicit opt-out request
  - other           — anything that doesn't fit cleanly
"""
from __future__ import annotations
import json
import logging
from typing import Optional
from app.config import settings
from app.services.ai_client import chat_with_system, MODEL_FAST

log = logging.getLogger("bmp.reply_classifier")

VALID_SENTIMENTS = {
    "interested", "objection", "out_of_office",
    "wrong_person", "unsubscribe", "other",
}

SYSTEM_PROMPT = """You are classifying a prospect's reply to a cold outreach email.
Return STRICT JSON with two fields:
  - sentiment: one of [interested, objection, out_of_office, wrong_person, unsubscribe, other]
  - summary:   one short sentence (under 100 chars) capturing the reply's intent

Definitions:
- "interested": they want pricing, want to schedule, ask a buying question, or otherwise signal forward motion
- "objection": "not now", "we already have X", "no budget", or any pushback that needs a human response
- "out_of_office": auto-reply about vacation/OOO/being away
- "wrong_person": "I don't handle this", "talk to John instead", "wrong department"
- "unsubscribe": explicit opt-out — "remove me", "stop emailing", "take me off your list"
- "other": doesn't cleanly fit any of the above

Return ONLY the JSON object. No prose, no markdown fences."""


async def classify_reply(body_text: str, subject: str = "") -> Optional[dict]:
    """Classify an inbound reply. Returns {sentiment, summary} or None on error.

    Always best-effort — never raises. The caller treats None as
    "not classified yet" and the UI surfaces a neutral badge.
    """
    if not (body_text or "").strip():
        return None
    if not settings.anthropic_api_key:
        return None

    # Cap input — we only need the first paragraph or two to classify.
    # Long quoted-thread replies still classify accurately on the top.
    snippet = (body_text or "").strip()[:2000]
    user_prompt = f"Subject: {(subject or '').strip()[:200]}\n\nReply:\n{snippet}"

    try:
        # Classification is a textbook Haiku fit — cheaper, faster, and
        # the system prompt is fixed so it caches across calls.
        text = (await chat_with_system(
            model=MODEL_FAST,
            system=SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=120,
            cacheable=True,
        )).strip()
        # Strip code fences if Claude added them despite instructions
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        text = text.strip()
        data = json.loads(text)
        sent = (data.get("sentiment") or "").strip().lower()
        if sent not in VALID_SENTIMENTS:
            sent = "other"
        summary = (data.get("summary") or "").strip()[:200]

        # Meter the AI call
        try:
            from app.services.credit_meter import meter_standalone as _meter_ai
            await _meter_ai(
                action_type="ai_reply_classify",
                action_ref=f"reply_classify",
                metadata={"sentiment": sent},
            )
        except Exception:
            pass

        return {"sentiment": sent, "summary": summary}
    except Exception as e:
        log.warning(f"reply_classifier.classify_reply failed: {e}")
        return None
