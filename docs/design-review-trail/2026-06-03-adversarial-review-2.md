# Adversarial Review #2 — Engagement Engine Design Doc v2

**Date**: 2026-06-03
**Reviewer**: General-purpose agent (independent, no design context beyond v1 review notes)
**Target**: `docs/ENGAGEMENT_ENGINE_DESIGN.md` v2

---

## Part A — v2 fixes assessment

Of the 15 v1 findings:
- **9 fully pass**: #2 (locking), #3 (atomic cost), #7 (stale action), #10 (summary stale), #11 (BYO AI JSON), #13 (signal types), #14 (timezone/TCPA), #15 (cutover owner), and the raw_data + tenant_id consistency honorable mentions
- **5 partial pass**: #1 (prompt injection — re-validation gap at dispatch), #4 (FSM — requires_status not enforced), #6 (observations — cascade behavior undefined), #8 (deliverability — index bug), #12 (key rotation — flow undocumented)
- **1 introduces critical bug**: #8 NOW()-based partial index will fail migration

## Part B — NEW issues introduced by v2

### Hard Blockers for Phase 1 (must fix)
- **B1**: `email_suppressions` partial index uses `WHERE expires_at IS NULL OR expires_at > NOW()` — Postgres rejects non-IMMUTABLE in partial index predicate. **Migration won't run.**
- **B2**: Phase transition trigger doesn't check `requires_status` column — column is dead, transitions like `meeting_set → post_meeting_nurture` don't actually verify the gate.
- **B3**: Recipient-lock trigger fires only on action INSERT/UPDATE, not on contact email change. Stale scheduled actions can dispatch to wrong recipient. Need dispatcher re-check.
- **B10**: Recipient-lock blocks legitimate BDR CC'ing different contact at same company. Need `manual + sent_by_user_id IS NOT NULL` exemption.
- **B13**: `day_offset` mode-consistency CHECK contains dead placeholder subquery; trigger function not specified.

### Strong recommendations
- **B4**: Lookup tables use VARCHAR PKs — 30-40% size overhead on 50M+ rows, slower FK lookups. Switch to SMALLINT surrogate PK + UNIQUE on code.
- **B5**: Workers cache lookup registries in memory. New row inserted at DB → workers don't know. Need NOTIFY/LISTEN or per-tick cache-miss re-check.
- **B7**: `outreach_owner` 3-value enum insufficient. Missing: paused, white_glove, disputed. Conflates routing with engageability.
- **B8**: Phase transition composite PK `(from, to, allowed_by)` requires dup rows for transitions allowed by both AI and BDR. Acceptable but document; consider bitmask alternative.
- **B9**: Advisory locks held across 5-30s LLM calls block parallelism. Release during LLM, re-acquire for state mutation.
- **B11**: Dedupe counter insert race needs documented `INSERT ... ON CONFLICT DO UPDATE` pattern.
- **B12**: Email `sent_today` increment needs atomic UPDATE-WHERE pattern; reset cron timezone undefined.

## Part C — Still missing in v2

- **C1** (HIGH): Inbound email reply routing path is undesigned. `signal_type='email_reply'` exists in lookup but no ingestion mechanism (webhook? IMAP poll? reply-to address scheme?). **Engine can't function as designed without this.**
- **C2**: Dispatcher backpressure metrics + alerts not defined.
- **C4**: `ai_decisions` table partitioning not decided — will hit Postgres vacuum issues at 5M+ rows/month.
- **C5**: `engagement_score` writer still ownerless — column added in v2 but no decision_type writes it.
- **C6-C11**: Minor edge cases, mostly tracked for relevant phases.

## Overall Recommendation

**v2 is NOT ready for Phase 1 as-is.** Hard blockers must be fixed before migration code is written.

After v3 lands the 7 hard blockers + 7 strong recommendations, design is defensible for Phase 1.

Estimated time to v3: 0.5–1 day of focused revision.

