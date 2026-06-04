# Adversarial Review #1 — Engagement Engine Design Doc

**Date**: 2026-06-03
**Reviewer**: General-purpose agent (independent, no prior context)
**Target**: `docs/ENGAGEMENT_ENGINE_DESIGN.md` v1 (commit: pre-revision)
**Findings**: 15 numbered + 7 honorable mentions

---

## Top 5 Critical

1. **Prompt injection via `engagements.notes` and `signals.raw_data_json`** — BDR freeform + scraped third-party content flow into LLM prompts unchanged. Prospect-controlled text can hijack AI to write attacker email into `action.recipient_email` (never validated against `contact.email`).

2. **No worker-level locking; duplicate decisions guaranteed** — Two `decision_maker` instances fetch same signal → 2 LLM calls → 2 actions. Idempotency key uses `decision.id` which differs per instance. Fix: `SELECT ... FOR UPDATE SKIP LOCKED` + advisory locks + idempotency key changes to `sig-{id}` only.

3. **`monthly_ai_cost_usd` non-atomic** — Pre-call read + post-call write opens window for runaway burns. Provider-no-usage cases (Ollama, vLLM, Bedrock errors) never increment. No reset cron defined.

4. **Phase enum + status enum allow illegal combinations; no state machine** — Schema allows `(phase='customer', status='terminal', terminal_reason='lost')` nonsense. `recommend_phase_transition` decision has freeform output — AI can hallucinate any transition.

5. **CHECK constraint expansion is NOT safe** — `ALTER TABLE actions DROP CONSTRAINT + ADD CONSTRAINT` takes ACCESS EXCLUSIVE, blocks all I/O, scans whole table. Rule #1 (additive-only) violated in practice every time a channel/source/type is added.

## High (5)

6. `observations` tied to `engagement_id` orphans polling on re-engagement (which the 12-month nurture mandate WILL hit).
7. TOCTOU race: action scheduled at T0, contact replies at T0+30min, dispatcher still sends at T0+1h ignoring reply.
8. **Email deliverability + sender warmup completely missing**. Will trigger Resend suspension within weeks of scaled use.
13. Signal type enum missing operational categories: bounce/complaint/unsubscribe/sms_opt_out/contact_left_company/company_acquired/competitor_signed/payment_failed.
14. **TCPA violation risk** — SMS outside 8am-9pm local time is fineable. No timezone on contacts.

## Medium (5, phase-specific)

9. Polling cadence claim vs cost model mismatch (weekly vs daily).
10. AI summary refresh strategy undefined — 6-day stale summaries drive expensive decisions.
11. BYO AI JSON-mode reliability varies wildly (Ollama returns valid JSON ~70%).
12. API key encryption/rotation hand-waved.
15. Cutover shared state: both old + new engines see same contacts → dual-send risk.

## Honorable Mentions (7)

- `SERIAL` → `BIGINT IDENTITY` on signals (highest-volume, INT4 exhausts in <10y).
- Tenant FK cross-tenant consistency triggers needed (RLS doesn't catch bad tenant_id on FK target).
- `actions.dedupe_window` missing — AI can decide 3 sends same day if 3 signals fire.
- `engagement_score` has no owner.
- `playbook_actions.day_offset` meaningful only when `mode='linear_sequence'`.
- LinkedIn `raw_data_json` copyright/ToS risk — store hashes/references, not full posts.
- No tenant_id consistency check on FK targets.

---

## Disposition

All 5 critical + all 5 high + all 7 honorable mentions are being incorporated into the revised design doc.

The 5 medium findings are tracked as Phase-specific acceptance criteria (Phase 3, 4, 4, 4, 7 respectively) — they don't change the v1 schema but must be addressed before their phase closes.

This review trail is preserved so the design-evolution rationale is auditable.

