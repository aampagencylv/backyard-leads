from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey, Boolean, Table
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from app.database import Base


# Many-to-many: companies <-> tags
company_tags = Table(
    "company_tags",
    Base.metadata,
    Column("company_id", Integer, ForeignKey("companies.id"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)

    # Standardized signature fields
    first_name = Column(String(80), nullable=False, default="")
    last_name = Column(String(80), nullable=False, default="")
    nickname = Column(String(120), nullable=False, default="")
    phone_number = Column(String(40), nullable=False, default="")
    scheduling_url = Column(String(255), nullable=False, default="")

    sending_enabled = Column(Boolean, default=False)
    role = Column(String(20), nullable=False, default="sales_rep")  # super_admin, admin, sales_rep, read_only

    # Guided onboarding tour progress. 0 = not started, N = currently on step N (1-10),
    # 99 = skipped, 100 = completed. New users auto-start on first login until 99 or 100.
    onboarding_step = Column(Integer, nullable=False, default=0)

    # Daily morning brief — TZ-aware delivery via the cron loop in main.py.
    # brief_hour is local-time hour (0-23). We send when local time crosses
    # that hour AND last_brief_sent_at is not today (UTC). Default 7am
    # works for most US-based BDR workflows.
    brief_enabled = Column(Boolean, default=True, nullable=False)
    brief_hour = Column(Integer, default=7, nullable=False)
    timezone = Column(String(80), default="America/Phoenix", nullable=False)
    last_brief_sent_at = Column(DateTime, nullable=True)

    # Twilio Voice — per-rep phone number + SDK identity
    twilio_phone_number = Column(String(40), nullable=True)  # E.164 format, e.g. +17025551234
    twilio_identity = Column(String(80), nullable=True)      # SDK identity, e.g. "bmp_user_3"

    # Google OAuth — per-user calendar integration for native scheduler.
    # We only store the refresh_token long-term; access tokens are
    # exchanged on demand and never persisted. `google_calendar_id` is
    # the dedicated "BMP Discovery Calls" calendar we auto-create on
    # first connect — booking events go there so disconnecting the
    # integration doesn't touch the user's personal events.
    google_email = Column(String(255), nullable=True)
    google_refresh_token = Column(Text, nullable=True)
    google_calendar_id = Column(String(255), nullable=True)
    google_connected_at = Column(DateTime, nullable=True)
    # Public booking slug — appears in booking URL /book/{slug}. Defaults
    # to a kebab-case form of the user's first+last name on connect.
    booking_slug = Column(String(80), nullable=True, unique=True, index=True)

    # Booking-host routing — when a BDR's outbound assets (audit reports,
    # email signatures, sidebar "Schedule a meeting" button) need a
    # booking URL, we substitute this user's calendar slug instead of
    # the BDR's own. Lets an admin centralize "Discovery Call" bookings
    # on their calendar without making every BDR a calendar owner.
    # NULL = use the BDR's own calendar (default).
    default_booking_host_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    # Per-rep dial preferences
    # 'browser' = WebRTC via Twilio.Device (default; needs headset + good internet)
    # 'bridge'  = CallRail-style: Twilio rings personal_phone_number first, bridges to prospect
    dial_mode = Column(String(20), nullable=False, default="browser")
    personal_phone_number = Column(String(40), nullable=True)  # E.164; required when dial_mode='bridge'

    # Custom voicemail greeting — if set, plays this audio file instead of TTS.
    # Stored as a relative URL served by the app (e.g. /uploads/voicemail/3/greeting.mp3)
    voicemail_greeting_url = Column(String(500), nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    searches = relationship("Search", back_populates="user")
    activities = relationship("Activity", back_populates="user", foreign_keys="[Activity.user_id]")
    tasks = relationship("Task", back_populates="user")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class Search(Base):
    __tablename__ = "searches"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    keyword = Column(String(255), nullable=False)
    location = Column(String(255), nullable=False)
    results_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="searches")
    companies = relationship("Company", back_populates="search")


class Company(Base):
    """A business we discovered/imported. Holds firmographic + enrichment data."""
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, index=True)
    search_id = Column(Integer, ForeignKey("searches.id"), nullable=True)

    # Firmographic data (from map scrape)
    name = Column(String(500), nullable=False)
    phone = Column(String(50))
    website = Column(String(500))
    # Normalized canonical domain extracted from `website` — used for dedupe so
    # two Company rows can't accidentally exist with the same effective site.
    # Populated automatically on create via domain_utils.normalize_domain().
    domain = Column(String(255), index=True, nullable=True)
    address = Column(String(500))
    city = Column(String(255))
    state = Column(String(100))
    rating = Column(Float)
    review_count = Column(Integer)
    business_type = Column(String(255))

    # Enrichment data (website analysis)
    enriched = Column(Boolean, default=False)
    site_speed_score = Column(Float)
    has_blog = Column(Boolean)
    has_social_links = Column(Boolean)
    last_review_date = Column(String(100))
    mobile_friendly = Column(Boolean)
    has_ssl = Column(Boolean)
    tech_stack = Column(Text)
    problems_found = Column(Text)
    enrichment_summary = Column(Text)

    # CRM lifecycle
    status = Column(String(50), default="new")  # new, pursuing, sequencing, contacted, replied, qualified, converted, not_interested
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Convenience flags (derived but cached)
    email_generated = Column(Boolean, default=False)
    email_sent = Column(Boolean, default=False)
    pushed_to_hubspot = Column(Boolean, default=False)
    sequence_started_at = Column(DateTime, nullable=True)

    # Company intel (from Netrows LinkedIn enrichment)
    employee_count = Column(Integer, nullable=True)
    company_size = Column(String(50), nullable=True)  # e.g. "11-50"
    industry = Column(String(255), nullable=True)
    linkedin_url = Column(String(500), nullable=True)
    founded = Column(String(20), nullable=True)
    company_description = Column(Text, nullable=True)
    specialties = Column(String(500), nullable=True)
    follower_count = Column(Integer, nullable=True)

    # Social profiles auto-scraped from the company's website during enrichment
    # (website_intel._check_social). First-class columns rather than custom
    # fields because every business potentially has these and they're
    # platform-derivable, not tenant-specific data entry.
    facebook_url = Column(String(500), nullable=True)
    instagram_url = Column(String(500), nullable=True)
    youtube_url = Column(String(500), nullable=True)
    tiktok_url = Column(String(500), nullable=True)
    # Annual revenue waits on ZoomInfo integration. When the BYO key
    # adapter ships, this column populates from /api/v1/organizations/...

    # Cached enrichment payloads from Netrows premium endpoints.
    # JSON-serialized; parsed at render time so schema doesn't churn
    # whenever Netrows adds a field. TTL: 30 days (firmographics) /
    # 7 days (Instagram posts — these turn over faster).
    company_insights_json = Column(Text, nullable=True)
    insights_fetched_at = Column(DateTime, nullable=True)
    instagram_posts_json = Column(Text, nullable=True)
    instagram_posts_fetched_at = Column(DateTime, nullable=True)

    # Tier 2 Netrows caches.
    # SimilarWeb traffic — `monthly_visits` is denormalized for filtering /
    # lead-scoring without parsing the JSON blob. <100 visits/mo signals a
    # parked / abandoned site → drop from cold-outreach cadences.
    similarweb_json = Column(Text, nullable=True)
    similarweb_fetched_at = Column(DateTime, nullable=True)
    monthly_visits = Column(Integer, nullable=True)
    # Tech-stack detection (BuiltWith-equivalent).
    tech_stack_json = Column(Text, nullable=True)
    tech_stack_fetched_at = Column(DateTime, nullable=True)
    # Yelp profile cache — populated on-demand via "Yelp" button.
    yelp_json = Column(Text, nullable=True)
    yelp_fetched_at = Column(DateTime, nullable=True)
    # Indeed hiring activity — populated on-demand.
    indeed_jobs_json = Column(Text, nullable=True)
    indeed_jobs_fetched_at = Column(DateTime, nullable=True)

    # Google Maps reviews cache (Netrows /google-maps/reviews)
    google_place_id = Column(String(80), nullable=True)
    reviews_json = Column(Text, nullable=True)  # JSON array of reviews with owner_reply parsed out
    reviews_fetched_at = Column(DateTime, nullable=True)

    # Lead score v2 (fit × intent). Lazy-computed by lead_scorer.py — recomputed
    # on read when the cached value is older than _STALE_AFTER (default 1h).
    # Replaces the old "3+ opens or any click" Hot Leads heuristic.
    #   fit:      firmographic / ICP-match score (0-100)
    #   intent:   engagement signal score (0-100, time-decayed)
    #   combined: blended fit×intent score (0-100) — what the dashboard ranks by
    #   tier:     'burning'|'hot'|'warm'|'cool'|'cold' bucket from combined
    #   components_json: per-component breakdown for the score-tooltip UI
    lead_score = Column(Integer, default=0, nullable=False, index=True)
    lead_score_fit = Column(Integer, default=0, nullable=False)
    lead_score_intent = Column(Integer, default=0, nullable=False)
    lead_score_tier = Column(String(20), default="cold", nullable=False, index=True)
    lead_score_components = Column(Text, nullable=True)
    lead_score_updated_at = Column(DateTime, nullable=True)

    # User-defined custom fields. JSON dict {definition_key: value}. Field
    # definitions live in custom_field_definitions; this column just holds
    # the values. Both BMP defaults (facebook_page, instagram_page,
    # annual_revenue) and tenant-defined fields share this storage.
    custom_fields_json = Column(Text, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    search = relationship("Search", back_populates="companies")
    contacts = relationship("Contact", back_populates="company", cascade="all, delete-orphan", order_by="Contact.is_primary.desc(), Contact.id")
    deals = relationship("Deal", back_populates="company", cascade="all, delete-orphan", order_by="Deal.created_at.desc()")
    activities = relationship("Activity", back_populates="company", cascade="all, delete-orphan", order_by="Activity.created_at.desc()")
    tasks = relationship("Task", back_populates="company", cascade="all, delete-orphan", order_by="Task.due_date")
    tags = relationship("Tag", secondary=company_tags, back_populates="companies")
    assigned_user = relationship("User", foreign_keys=[assigned_to])


class Contact(Base):
    """A person at a Company. Multiple contacts per company supported."""
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)

    first_name = Column(String(80), default="")
    last_name = Column(String(80), default="")
    title = Column(String(255))
    email = Column(String(255), index=True)
    phone = Column(String(50))
    linkedin_url = Column(String(500))

    is_primary = Column(Boolean, default=False)
    notes = Column(Text)

    # Email validation / suppression
    email_status = Column(String(20), default="unknown")  # unknown, valid, invalid, bounced
    unsubscribed_at = Column(DateTime, nullable=True)
    unsubscribe_token = Column(String(64), index=True, nullable=True)

    # SMS opt-out — TCPA compliance: when a contact replies STOP/UNSUBSCRIBE
    # to one of our texts, we set do_not_text=True and refuse all future SMS.
    do_not_text = Column(Boolean, default=False, nullable=False)
    do_not_text_at = Column(DateTime, nullable=True)

    # Phone-type cache from Twilio Lookup v2 — populated lazily on first send attempt.
    # Values: 'mobile' (iMessage/SMS works), 'landline' (refuse send), 'voip',
    # 'unknown' (lookup not yet attempted), 'error' (lookup failed; treat as unknown).
    phone_type = Column(String(20), nullable=True)
    phone_type_checked_at = Column(DateTime, nullable=True)
    phone_carrier = Column(String(80), nullable=True)  # e.g. "Verizon Wireless"

    # Missive conversation linkage — populated the first time a BDR
    # opens this contact's thread in Missive (sidebar pushes it back
    # to us). Lets status-change hooks fire write-back actions like
    # 'apply Replied label' or 'add comment' against the right thread.
    missive_conversation_id = Column(String(64), nullable=True, index=True)
    missive_conversation_seen_at = Column(DateTime, nullable=True)

    # Personalization context cache (Netrows /people/posts)
    recent_posts_json = Column(Text, nullable=True)  # JSON array of {text, posted_at, url, likes}
    posts_fetched_at = Column(DateTime, nullable=True)

    # Tenant-defined custom fields. JSON dict {definition_key: value}.
    # Definitions live in custom_field_definitions where entity_type='contact'.
    custom_fields_json = Column(Text, nullable=True)

    # Full LinkedIn profile cache (Netrows /people/profile-by-url).
    # Triggered on-demand from the contact card. Auto-fills title +
    # first/last name when those are empty; full payload kept here for
    # future "view profile" UI without a fresh API hit.
    linkedin_profile_json = Column(Text, nullable=True)
    linkedin_profile_fetched_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    company = relationship("Company", back_populates="contacts")
    emails = relationship("GeneratedEmail", back_populates="contact", cascade="all, delete-orphan", order_by="GeneratedEmail.sequence_order")

    @property
    def full_name(self) -> str:
        return f"{self.first_name or ''} {self.last_name or ''}".strip()


class Deal(Base):
    """A revenue opportunity at a Company. Multiple deals per company supported."""
    __tablename__ = "deals"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)

    name = Column(String(255), nullable=False)
    value = Column(Float, nullable=True)  # monthly retainer value
    stage = Column(String(50), default="prospecting")
    pipeline = Column(String(50), default="default")
    probability = Column(Integer, default=0)  # 0-100
    expected_close_date = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    lost_reason = Column(String(255), nullable=True)

    # Snooze / reactivation
    snoozed_until = Column(DateTime, nullable=True)
    snooze_reason = Column(Text, nullable=True)
    stage_before_snooze = Column(String(50), nullable=True)  # restore to this stage on wake

    # BMP Package system
    package = Column(String(50), nullable=True)
    contract_months = Column(Integer, default=6)

    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    company = relationship("Company", back_populates="deals")
    assigned_user = relationship("User", foreign_keys=[assigned_to])


class GeneratedEmail(Base):
    """
    A step in a multi-channel outreach sequence.
    Despite the table name, this now handles emails, LinkedIn messages, calls, texts, and custom tasks.
    """
    __tablename__ = "generated_emails"

    id = Column(Integer, primary_key=True, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)

    # Step type: email/imessage = auto-sendable by engine; call/linkedin = creates BDR Task
    step_type = Column(String(20), default="email")  # email, imessage, linkedin, call, text, custom
    subject = Column(String(500), nullable=False)  # email subject, or task title for non-email
    body = Column(Text, nullable=False)  # email body, iMessage text, LinkedIn message, call talk-track, task notes
    email_type = Column(String(50), default="cold")  # cold, follow_up_1, follow_up_2, breakup, linkedin_connect, linkedin_message, call, text, imessage
    sequence_order = Column(Integer, default=1)
    send_delay_days = Column(Integer, default=0)
    scheduled_send_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)  # when email was sent or task was completed
    is_sent = Column(Boolean, default=False)  # for emails: actually sent. For tasks: completed by BDR.
    paused_at = Column(DateTime, nullable=True)
    problems_referenced = Column(Text)

    # Sequence engine v1 (auto-execution)
    # Skip conditions evaluated at run-time. JSON array of strings: 'no_email', 'no_phone',
    # 'no_linkedin', 'opted_out', 'landline'. When any condition matches, the step is skipped
    # (skipped_at set + Activity logged) and the next step continues normally.
    skip_if_json = Column(Text, nullable=True)
    skipped_at = Column(DateTime, nullable=True)
    skip_reason = Column(String(80), nullable=True)
    # auto_execute=True: engine fires the step automatically (email, imessage)
    # auto_execute=False: engine creates a Task on the BDR (call, linkedin)
    auto_execute = Column(Boolean, default=False, nullable=False)
    # Group steps into named sequences on the same contact: 'main', 'post_call', 'reactivation'
    sequence_label = Column(String(40), default="main", nullable=False)
    # Channel-specific payload (e.g. iMessage already-personalized text, call talk-track template)
    payload_json = Column(Text, nullable=True)
    # Resulting Task id when auto_execute=False — lets us check completion to advance the sequence
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)

    # Which User sent (or will send) this email. Used for the per-sender daily
    # send-cap that protects deliverability — if a user has already hit their
    # cap today, the engine defers their pending steps to tomorrow 8am.
    sent_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Token used in the Reply-To address (`r-<token>@inbound.bymp.com`) so when
    # the prospect replies, the inbound webhook can attribute the reply back to
    # this exact email row. Generated at insert time. Tokens never expire — they
    # remain valid as long as the row exists, so a months-late reply still
    # threads correctly.
    reply_token = Column(String(40), unique=True, index=True, nullable=True)

    # Resend webhook event timestamps. Populated by /api/send/webhook/resend
    # as events arrive. opened_at is the FIRST open; open_count increments
    # on each subsequent open. bounced_at/complained_at being set is what
    # the engine uses to skip a contact's remaining steps.
    delivered_at = Column(DateTime, nullable=True)
    opened_at = Column(DateTime, nullable=True)
    open_count = Column(Integer, default=0, nullable=False)
    bounced_at = Column(DateTime, nullable=True)
    complained_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    contact = relationship("Contact", back_populates="emails")
    company = relationship("Company")


class Activity(Base):
    """Timeline entry on a Company (and optionally a Contact or Deal)."""
    __tablename__ = "activities"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Types: note, call, meeting, email_sent, email_opened, email_clicked, email_bounced,
    #        email_replied, status_change, enriched, linkedin_message, task_created,
    #        task_completed, deal_update, contact_added, sequence_created, sequence_paused
    activity_type = Column(String(50), nullable=False)
    content = Column(Text, default="")
    metadata_json = Column(Text, nullable=True)

    # Call-specific columns (populated when activity_type='call' or 'voicemail')
    twilio_call_sid = Column(String(50), nullable=True, index=True)
    call_duration_seconds = Column(Integer, nullable=True)
    call_direction = Column(String(20), nullable=True)  # 'outbound' | 'inbound'
    call_outcome = Column(String(40), nullable=True)
    # outcome values: connected, voicemail, no_answer, busy, declined,
    #                 wrong_number, gatekeeper, failed
    recording_url = Column(String(500), nullable=True)
    transcript = Column(Text, nullable=True)
    call_summary = Column(Text, nullable=True)  # AI-generated takeaways
    # Structured speaker-diarization output from Deepgram.
    # Array of {speaker:int, start:float, end:float, text:str}. Powers
    # the dual-channel call-recording waveform on the dashboard.
    diarized_segments_json = Column(Text, nullable=True)
    # {"rep_words": int, "prospect_words": int, "rep_pct": float,
    #  "prospect_pct": float} — computed at transcription time.
    talk_ratio_json = Column(Text, nullable=True)

    # Call review / coaching feedback (admin rates BDR calls)
    call_rating = Column(Integer, nullable=True)  # 1-5 stars
    call_feedback = Column(Text, nullable=True)  # Admin written feedback
    rated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    rated_at = Column(DateTime, nullable=True)

    # AI-classified sentiment for email_replied activities. One of:
    # 'interested', 'objection', 'out_of_office', 'wrong_person',
    # 'unsubscribe', 'other'. Populated async by reply_classifier after
    # the reply is logged. NULL means not classified (yet, or
    # classification disabled).
    reply_sentiment = Column(String(20), nullable=True, index=True)
    reply_sentiment_summary = Column(Text, nullable=True)  # one-line AI gist

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    company = relationship("Company", back_populates="activities")
    user = relationship("User", back_populates="activities", foreign_keys=[user_id])


class RuntimeConfig(Base):
    """Single-row org-level config that can be updated from the Settings UI.
    Keys here override env-var defaults (so users can rotate API keys without
    SSHing into the server).
    Always row id=1.
    """
    __tablename__ = "runtime_config"

    id = Column(Integer, primary_key=True, default=1)
    netrows_api_key = Column(Text, nullable=True)

    # Twilio credentials — Account SID + Auth Token are required;
    # API Key/Secret + TwiML App SID are needed once we move to SDK-based dialing
    twilio_account_sid = Column(Text, nullable=True)
    twilio_auth_token = Column(Text, nullable=True)
    twilio_api_key_sid = Column(Text, nullable=True)
    twilio_api_key_secret = Column(Text, nullable=True)
    twilio_twiml_app_sid = Column(Text, nullable=True)

    # Deepgram — telephony-grade transcription (Whisper alt; better on phone audio,
    # native speaker diarization for talk-to-listen ratio coaching).
    deepgram_api_key = Column(Text, nullable=True)

    # Blooio — iMessage automation (and optionally SMS via your own Twilio creds).
    # Used as the primary "Message" channel; iMessage gets 3-4× higher response
    # rates than SMS for B2B cold outreach in iPhone-heavy markets.
    blooio_api_key = Column(Text, nullable=True)

    # Org-wide messaging tone / strategic direction. Prepended to every AI
    # generation system prompt (cold email, follow-up, iMessage, post-call)
    # so the team can steer the voice and the angle without code changes.
    # Empty = use the in-code default.
    messaging_direction = Column(Text, nullable=True)

    # Blooio webhook signing secret (whsec_…) — used to HMAC-verify inbound
    # webhook payloads. Without this, our /api/blooio/inbound endpoint
    # accepts any request that knows the URL.
    blooio_signing_secret = Column(Text, nullable=True)

    # Resend webhook signing secret (whsec_…) — used to HMAC-verify the inbound
    # email webhook (/api/email/inbound) where prospect replies route after we
    # set Reply-To: r-<token>@go.bymp.com. DB-first; falls back to
    # settings.resend_webhook_secret env var if this is empty.
    resend_webhook_secret = Column(Text, nullable=True)

    # Google Maps API key — powers /find-leads (Places API + nearby search) and
    # the campaign runner's geo-targeted scrapes. Platform-tier: super_admin
    # only. DB-first with env fallback so rotation doesn't need a redeploy.
    google_maps_api_key = Column(Text, nullable=True)

    # Org-wide brand. Single source of truth for the org's identity —
    # primary color, secondary (accent) color, soft tint, logo image,
    # company display name. Every surface that needs branding (emails,
    # audit reports, booking pages, app UI accents) falls back to
    # these values when its own override is empty.
    #
    # Hierarchy:
    #   email / app UI         → always use org brand
    #   audit report           → use audit_report_* if set, else brand_*
    #   booking page (per-user)→ use SchedulingConfig.* if set, else brand_*
    brand_primary_color = Column(String(20), nullable=False, default="#E65100")
    brand_secondary_color = Column(String(20), nullable=False, default="#1B5E20")
    brand_accent_bg_color = Column(String(20), nullable=False, default="#FFF8F0")
    brand_logo_url = Column(Text, nullable=True)
    brand_company_name = Column(String(120), nullable=False, default="Backyard Marketing Pros")
    # Homepage URL used in email signatures + footers. White-label tenants
    # point this at their own marketing site.
    brand_website_url = Column(Text, nullable=False, default="https://backyardmarketingpros.com")

    # Editable middle pipeline stages — JSON array of
    # {key, name, probability, color}. NULL means "use defaults". System
    # stages (in_sequence/closed_won/closed_lost/snoozed) are NEVER stored
    # here — they're fixed in code because they have special wiring.
    pipeline_stages_json = Column(Text, nullable=True)

    # Autopilot send window — per-channel hours, basis radio, optional
    # rep-presence gate. The legacy autopilot_send_start_hour /
    # _end_hour / _days_json columns are kept around for backwards
    # compatibility but only read on a tenant that hasn't touched the
    # newer config yet (the migration copies them into the email +
    # imessage rows on first read).
    autopilot_send_start_hour = Column(Integer, nullable=False, default=8)
    autopilot_send_end_hour   = Column(Integer, nullable=False, default=19)
    autopilot_send_days_json  = Column(Text, nullable=True)

    # Window basis — which clock the hours apply to:
    #   "contact"   — contact's local timezone (default; right for cold outreach)
    #   "rep"       — assigned rep's saved timezone (only fire during the rep's workday)
    #   "strictest" — only fire when *both* contact-local AND rep-local
    #                 are inside their respective windows. The right choice
    #                 when you want a human available to reply.
    autopilot_basis = Column(String(20), nullable=False, default="contact")

    # Per-channel hours. iMessage defaults narrower (8am-5pm) than email
    # because someone needs to be online to reply.
    autopilot_email_start_hour    = Column(Integer, nullable=False, default=8)
    autopilot_email_end_hour      = Column(Integer, nullable=False, default=19)
    autopilot_email_days_json     = Column(Text, nullable=True)  # null = every day
    autopilot_imessage_start_hour = Column(Integer, nullable=False, default=8)
    autopilot_imessage_end_hour   = Column(Integer, nullable=False, default=17)
    autopilot_imessage_days_json  = Column(Text, nullable=True)

    # Rep-presence gating — when enabled, the engine only fires a step
    # if the assigned rep was active in the app within the past 15min.
    # Requires PWA push / heartbeat work that hasn't shipped yet, so
    # the checkbox is disabled in the UI for now.
    autopilot_respect_rep_presence = Column(Boolean, nullable=False, default=False)

    # Audit-report branding — per-surface overrides on top of org brand.
    # Empty → render falls back to brand_logo_url (footer) or no banner
    # (header). Set explicitly when you want an audit-specific image
    # that differs from the org logo.
    audit_report_header_url = Column(Text, nullable=True)
    audit_report_logo_url = Column(Text, nullable=True)

    # Audit-report side panel content (left + right of the centered report
    # body). Each side independently supports an image URL + a short
    # message. When BOTH sides are empty, the report renders in its
    # original single-column centered layout; otherwise it switches to a
    # 3-column grid with the sidebars (collapsing to stacked on mobile).
    audit_left_image_url = Column(Text, nullable=True)
    audit_left_message = Column(Text, nullable=True)
    audit_right_image_url = Column(Text, nullable=True)
    audit_right_message = Column(Text, nullable=True)

    # Scheduler selector for the "Schedule a Discovery Call" CTA on the
    # audit report. Org-wide setting; the whole team's audits use the
    # same scheduler.
    #   'iclosed' → existing iClosed booking URL (settings.iclosed_booking_url)
    #   'native'  → /book/{slug} for the picked rep (audit_native_user_id)
    #   'custom'  → audit_custom_url verbatim
    audit_scheduler_type = Column(String(20), nullable=False, default="iclosed")
    audit_native_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    audit_custom_url = Column(Text, nullable=True)

    # Apollo BYO-key — the ONE customer-supplied integration in the SaaS
    # model. Tenants who already pay Apollo can plug their key in to unlock
    # decision-maker contacts + direct dials for verticals where Apollo's
    # database wins (B2B SaaS, mid-market, tech). Per-record cost stays on
    # the tenant's Apollo bill; we charge a small orchestration fee in
    # credits (see credit_meter RATE_CARD enrich_apollo). Tenant-tier:
    # admins set + rotate this from Settings.
    apollo_api_key = Column(Text, nullable=True)

    # ZoomInfo BYO integration — also tenant-tier, also customer-supplied.
    # ZoomInfo uses PKI authentication: tenants register an app in their
    # ZoomInfo developer portal, get a client_id + RSA private key + the
    # account email (username). Every API call we mint a fresh JWT signed
    # with the private key, exchange it at /authenticate for an access
    # token (24h validity), and use that for the actual data calls.
    # Cached access token lives here too so re-mint isn't every call.
    zoominfo_username = Column(Text, nullable=True)         # account email
    zoominfo_client_id = Column(Text, nullable=True)
    zoominfo_private_key = Column(Text, nullable=True)      # PEM-format RSA key
    zoominfo_access_token = Column(Text, nullable=True)     # cached JWT, refreshed on expiry
    zoominfo_token_expires_at = Column(DateTime, nullable=True)

    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class TrackingLink(Base):
    """One-shot URL wrapper for tracking email click-throughs (and later,
    site visits via the bmp_visitor cookie). Each <a href> in an outgoing
    email is rewritten to /t/{token} which 302s to destination_url after
    logging the click + dropping the visitor cookie."""
    __tablename__ = "tracking_links"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String(32), unique=True, index=True, nullable=False)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)
    email_id = Column(Integer, ForeignKey("generated_emails.id"), nullable=True)
    destination_url = Column(Text, nullable=False)
    label = Column(String(40), nullable=True)  # 'signature_website', 'signature_calendar', 'body_link', etc.
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    first_clicked_at = Column(DateTime, nullable=True)
    last_clicked_at = Column(DateTime, nullable=True)
    click_count = Column(Integer, default=0, nullable=False)


class PageView(Base):
    """A page on backyardmarketingpros.com that a tracked visitor loaded.
    Phase 2 of Website Visitor Tracking. Visitor is identified by the
    bmp_visitor cookie that we drop when they click /t/{token}.

    Same visitor_token can appear across multiple pageviews (one session)
    and across multiple sessions (returning visitor). Sessions are derived
    at query time: pageviews within 30 min of each other = one session."""
    __tablename__ = "page_views"

    id = Column(Integer, primary_key=True, index=True)
    visitor_token = Column(String(32), index=True, nullable=False)  # the TrackingLink.token
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)
    url = Column(Text, nullable=False)
    page_title = Column(String(500), nullable=True)
    referrer = Column(Text, nullable=True)
    user_agent = Column(String(300), nullable=True)
    ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

    # Event type:
    #   'pageview'        — default; just loaded a page
    #   'form_submit'     — form submission (HIGH signal — instant hot lead)
    #   'outbound_click'  — clicked a link to a different domain (Calendly, etc.)
    #   'tel_click'       — tapped a tel: link (mobile call intent)
    #   'mailto_click'    — clicked a mailto: link
    #   'custom'          — element with data-bmp-event="name" attribute clicked
    event_type = Column(String(30), default="pageview", nullable=False)
    event_label = Column(String(200), nullable=True)  # form id, link target, custom event name
    event_value = Column(Text, nullable=True)         # destination URL, form action, etc.


class SiteVisitorSession(Base):
    """An anonymous (or to-be-resolved) website visitor.

    Created when /api/track/pageview receives a beacon from a browser
    that doesn't match an existing TrackingLink (= the visitor didn't
    come from a tracked email link). The `bvid` is a UUID we drop in a
    cookie so we can recognize the visitor across pageviews + sessions.

    IP resolution happens async via app/services/visitor_resolver.py —
    when an IP lookup succeeds we backfill resolved_company_id +
    resolved_company_name + resolved_domain on the session row, then
    every future pageview from this bvid auto-attributes to that company.
    """
    __tablename__ = "site_visitor_sessions"

    id = Column(Integer, primary_key=True, index=True)
    bvid = Column(String(64), unique=True, nullable=False, index=True)

    # The first IP we saw for this bvid (mostly stable for office IPs;
    # mobile users hop IPs but the cookie is stable).
    ip = Column(String(64), nullable=True, index=True)
    user_agent = Column(String(300), nullable=True)

    # Reveal output — populated by the resolver service.
    resolved_company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)
    resolved_company_name = Column(String(255), nullable=True)
    resolved_domain = Column(String(255), nullable=True, index=True)
    resolved_at = Column(DateTime, nullable=True)
    # Heuristic — when True, this IP looks like a residential ISP and
    # the org name is the ISP not the company. Filter these out of the
    # Site Visitors list since they're noise.
    is_isp_ip = Column(Boolean, nullable=False, default=False)

    # Geo (best-effort)
    country = Column(String(8), nullable=True)
    region = Column(String(80), nullable=True)
    city = Column(String(120), nullable=True)

    pageview_count = Column(Integer, nullable=False, default=0)
    first_seen_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    last_seen_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)


class Tag(Base):
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    color = Column(String(20), default="#1B5E20")

    companies = relationship("Company", secondary=company_tags, back_populates="tags")


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    description = Column(String(500), nullable=False)
    due_date = Column(DateTime, nullable=True)
    completed = Column(Boolean, default=False)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    company = relationship("Company", back_populates="tasks")
    user = relationship("User", back_populates="tasks")


class SavedView(Base):
    """User-saved filter presets for Companies and Pipeline pages."""
    __tablename__ = "saved_views"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    page = Column(String(30), nullable=False)
    name = Column(String(100), nullable=False)
    filters_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# Many-to-many: campaigns <-> users (round-robin team)
campaign_members = Table(
    "campaign_members",
    Base.metadata,
    Column("campaign_id", Integer, ForeignKey("campaigns.id"), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),
)


class Campaign(Base):
    """
    Auto Pilot campaign. Defines target criteria, locations, and qualification rules.
    Runs autonomously: search → enrich → qualify → generate sequence.
    Moderate mode: sequences created but not auto-sent (BDR approves).
    """
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Target criteria
    business_types = Column(Text, nullable=False)  # JSON list: ["pool builders", "landscaping companies"]
    locations = Column(Text, nullable=False)  # JSON list: ["Phoenix, AZ", "Las Vegas, NV"]
    min_reviews = Column(Integer, default=20)
    max_reviews = Column(Integer, default=300)
    min_rating = Column(Float, default=3.5)
    must_have_website = Column(Boolean, default=True)

    # Qualification rules
    max_ai_visibility_score = Column(Integer, default=40)  # Below this = opportunity
    min_problems = Column(Integer, default=3)  # At least this many issues found
    contact_required = Column(Boolean, default=True)  # Must find an email to qualify

    # Sending rules
    max_prospects_per_day = Column(Integer, default=10)
    mode = Column(String(20), default="moderate")  # moderate = needs approval, full_auto = sends automatically

    # Dedup
    contact_cooldown_days = Column(Integer, default=90)  # Don't re-contact within this window

    # Round-robin state
    last_assigned_index = Column(Integer, default=0)  # Tracks which team member got the last lead

    # Campaign state
    status = Column(String(20), default="draft")  # draft, running, paused, completed
    # Progress tracking
    total_locations_searched = Column(Integer, default=0)
    total_prospects_found = Column(Integer, default=0)
    total_qualified = Column(Integer, default=0)
    total_sequences_created = Column(Integer, default=0)
    total_emails_sent = Column(Integer, default=0)
    total_replies = Column(Integer, default=0)

    # Execution state — tracks where we are in the campaign
    current_location_index = Column(Integer, default=0)  # Which location we're processing
    current_business_type_index = Column(Integer, default=0)  # Which business type within that location
    prospects_today = Column(Integer, default=0)  # Reset daily
    last_run_at = Column(DateTime, nullable=True)
    last_daily_reset = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    creator = relationship("User", foreign_keys=[created_by])
    members = relationship("User", secondary=campaign_members)


class CampaignLog(Base):
    """Log of every action Auto Pilot takes — full audit trail."""
    __tablename__ = "campaign_logs"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False)
    action = Column(String(50), nullable=False)  # searched, enriched, qualified, skipped, sequence_created, error
    detail = Column(Text, default="")
    company_id = Column(Integer, nullable=True)
    contact_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class SoSLookup(Base):
    """Cache for Secretary-of-State lookups (Phase 2 of the enrichment
    chain). Free public records, but each scrape costs latency + we want
    to be polite to the state's site, so cache aggressively. 30-day TTL
    matches how rarely SoS filings change.

    Keyed on (state, company_name_normalized) — name normalization
    strips entity suffixes (LLC, Inc, Corp), punctuation, and case so
    'Smith Pools, LLC' / 'SMITH POOLS' / 'Smith Pools Llc' all hit the
    same cache row.
    """
    __tablename__ = "sos_lookups"

    id = Column(Integer, primary_key=True, index=True)
    state = Column(String(4), nullable=False, index=True)  # 'FL', 'AZ', 'NV', ...
    company_name = Column(String(255), nullable=False, index=True)  # normalized form
    found = Column(Boolean, default=False, nullable=False)
    result_json = Column(Text, nullable=True)  # SoSResult dict serialized
    fetched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    expires_at = Column(DateTime, nullable=True, index=True)


class CustomFieldDefinition(Base):
    """User-defined custom fields on Companies + Contacts.

    Schema is intentionally simple: a flat list of field definitions
    (no per-field permissions, no validation rules beyond field_type).
    Storage is denormalized into Company.custom_fields_json /
    Contact.custom_fields_json — read/write goes through that JSON dict
    keyed by `key`.

    Pre-seeded BMP defaults include facebook_page, instagram_page,
    annual_revenue (company) and instagram_handle (contact). Admins can
    add more from Settings → Custom Fields.
    """
    __tablename__ = "custom_field_definitions"

    id = Column(Integer, primary_key=True, index=True)
    # 'company' or 'contact' — determines which entity surfaces this field
    entity_type = Column(String(20), nullable=False, index=True)
    # Slug used as the JSON key in custom_fields_json. Lowercase, snake_case.
    # Once a definition is created, key cannot change without orphaning data.
    key = Column(String(80), nullable=False)
    # Human-readable label shown in forms and detail views
    label = Column(String(120), nullable=False)
    # 'text' | 'textarea' | 'number' | 'url' | 'email' | 'phone' | 'date' | 'select'
    field_type = Column(String(20), default="text", nullable=False)
    # JSON list of options for field_type='select'
    options_json = Column(Text, nullable=True)
    # Helper text shown beneath the input (max 200 chars)
    helper_text = Column(String(200), nullable=True)
    # Render order in the UI (ascending)
    display_order = Column(Integer, default=100, nullable=False)
    # Soft-delete flag — false hides from forms but preserves field values
    is_active = Column(Boolean, default=True, nullable=False)
    # Pre-seeded BMP defaults set this so Settings UI can mark them as
    # "platform-provided" vs. tenant-created
    is_default = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)


class CampaignTarget(Base):
    """One (vertical, geo) pair inside a Campaign. God Mode treats every
    pair as its own concurrent producer with its own scrape cursor,
    pacing, weights, and counters — instead of marching through a single
    cross-product index one tick at a time.

    Sync rule: when Campaign.business_types or Campaign.locations changes,
    the cross-product is re-derived. New pairs get a fresh CampaignTarget;
    pairs that no longer exist are paused (kept for history, not deleted).
    """
    __tablename__ = "campaign_targets"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False, index=True)

    vertical = Column(String(255), nullable=False)
    location = Column(String(255), nullable=False)

    # Budget allocation: each target gets (weight / sum_active_weights) of
    # the campaign's daily prospect cap. Default 1 = round-robin.
    weight = Column(Integer, default=1, nullable=False)

    # active = picked up on every tick. paused = manually halted (or pair
    # was removed from the campaign config). exhausted = N consecutive
    # ticks returned no new results — auto-paused, surface in the brief.
    status = Column(String(20), default="active", nullable=False, index=True)

    # Lifetime counters
    contacts_enrolled = Column(Integer, default=0, nullable=False)
    sends_made = Column(Integer, default=0, nullable=False)
    replies_received = Column(Integer, default=0, nullable=False)
    credits_spent = Column(Float, default=0.0, nullable=False)

    # Today's counters — reset at UTC midnight via the runner's daily-reset check
    enrolled_today = Column(Integer, default=0, nullable=False)
    last_daily_reset = Column(DateTime, nullable=True)

    # Cursor: how far into Google Maps results we've scrolled. Each tick
    # advances; we never re-scrape the same offset on the same target.
    scrape_cursor = Column(Integer, default=0, nullable=False)

    # Pacing + exhaustion detection
    last_run_at = Column(DateTime, nullable=True)
    consecutive_empty_runs = Column(Integer, default=0, nullable=False)

    paused_reason = Column(String(255), nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)


class CampaignRun(Base):
    """One row per cron tick — what God Mode did during this batch.
    Drives the morning brief: 'while you slept, X targets ran, Y contacts
    enrolled, Z credits spent.' Also useful for debugging slow ticks."""
    __tablename__ = "campaign_runs"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False, index=True)

    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    finished_at = Column(DateTime, nullable=True)

    targets_processed = Column(Integer, default=0, nullable=False)
    contacts_enrolled = Column(Integer, default=0, nullable=False)
    sends_made = Column(Integer, default=0, nullable=False)
    replies_received = Column(Integer, default=0, nullable=False)
    credits_spent = Column(Float, default=0.0, nullable=False)

    error = Column(Text, nullable=True)
    summary_json = Column(Text, nullable=True)  # Per-target breakdown for the brief


class ApiKey(Base):
    """Personal API key — one per integration / external system. Owner
    is the User who created it; calls authenticated with this key act
    as that user (inheriting their role + scoping). Plaintext key is
    shown ONCE at creation; only the SHA-256 hash is stored.

    Format: 'pk_live_' + 64-char hex (32 random bytes).
    Header for auth: X-API-Key: pk_live_<hex>
    """
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(80), nullable=False)
    # SHA-256 of plaintext, hex-encoded. Index gives O(1) lookup on auth.
    key_hash = Column(String(64), unique=True, index=True, nullable=False)
    # First 12 chars of plaintext ('pk_live_AB1...') — shown in Settings
    # so users can recognize which key they're rotating without revealing
    # the full secret.
    key_prefix = Column(String(20), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    # Permission scope. v1 keys default to 'read' (search, get, summarize).
    # 'write' keys can additionally invoke MCP write tools (add note,
    # enroll in sequence, book meeting, etc.) — anything that mutates
    # the CRM. 'admin' is reserved for future tenant-level platform ops.
    # Existing keys created before this column existed are 'read' by
    # default for safety.
    scope = Column(String(20), default="read", nullable=False)


class Webhook(Base):
    """Outbound webhook subscription. When a subscribed event fires,
    the platform POSTs the event payload to `url` with HMAC-SHA256
    signature in X-Webhook-Signature header.

    Events (v1): company.created, contact.created, email.replied,
                 meeting.booked.
    More events lazily added as customers ask for them.
    """
    __tablename__ = "webhooks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(80), nullable=False)
    url = Column(String(500), nullable=False)
    # HMAC signing secret — generated server-side, shown once.
    # Customers store it in their endpoint to verify signatures.
    secret = Column(String(80), nullable=False)
    # JSON list of subscribed event names. Empty = all events.
    events_json = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    last_delivery_at = Column(DateTime, nullable=True)
    last_delivery_status = Column(Integer, nullable=True)  # HTTP status code
    last_delivery_error = Column(String(300), nullable=True)
    failure_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class AuditLogEntry(Base):
    """Immutable record of privileged actions across the platform.

    Captured for security review + SOC2 / enterprise compliance:
      - User management (invite, role change, deactivate, password reset,
        reassign companies/deals/tasks)
      - Runtime config changes (API key rotation, AI tone edit,
        webhook secret updates)
      - Destructive company actions (delete, merge)
      - Campaign / sequence start + pause + stop

    Denormalized fields (actor_email, target_label) preserve the row's
    meaning even after the actor or target is deleted. Append-only —
    never updated, never deleted.

    Indexed on (created_at), (actor_user_id), (action) for the most
    common admin queries: "what did Linda do this week", "who changed
    a role", "what happened on this date".
    """
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    actor_email = Column(String(255), nullable=True)   # Snapshot of actor's email at time of action
    actor_role = Column(String(20), nullable=True)     # Snapshot of actor's role at time of action

    # Action verb in dotted form: 'user.invited', 'user.role_changed',
    # 'runtime_config.updated', 'company.merged', etc.
    action = Column(String(80), nullable=False, index=True)

    # What was acted on. target_type maps to a model name; target_id is
    # the row's primary key when applicable; target_label is a human
    # snapshot (email, name, etc.) that survives the row's deletion.
    target_type = Column(String(40), nullable=True, index=True)
    target_id = Column(Integer, nullable=True)
    target_label = Column(String(255), nullable=True)

    # Context — JSON dict. For role changes: {"from": "sales_rep", "to": "admin"}.
    # For runtime_config: {"field": "twilio_account_sid", "before_set": True}.
    # NEVER stores the actual secret values — only mask presence/absence.
    metadata_json = Column(Text, nullable=True)

    # Request fingerprint
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(String(300), nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)


class CreditLedger(Base):
    """Per-action ledger of every billable thing we do.

    Two layers, one table:
      - credits_debited        — what we charge tenants (customer-facing)
      - raw_cost_usd            — what we actually pay vendors (admin/COGS view)

    Single-tenant today (BMP). When SaaS multi-tenancy lands, an `org_id`
    column gets added and existing rows backfill to org_id=1.

    Shim mode: rows are written but never enforced — no balance check
    blocks any action yet. Lets us collect 1-2 weeks of real cost data
    before we set retail SaaS prices.
    """
    __tablename__ = "credit_ledger"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    # Action type — one of the keys in credit_meter.RATE_CARD
    # (email_send, email_verify, ai_email_gen, ai_chat_turn, ai_reply_classify,
    #  enrich_netrows, enrich_hunter, enrich_apollo, phone_lookup,
    #  sms_send, voice_minute, scrape_yelp, scrape_maps)
    action_type = Column(String(40), nullable=False, index=True)
    # Free-form ref to the entity that triggered the action — e.g.
    # "generated_email:1234", "company:567", "contact:89".
    action_ref = Column(String(100), nullable=True)
    credits_debited = Column(Integer, nullable=False, default=0)
    raw_cost_usd = Column(Float, nullable=False, default=0.0)
    vendor = Column(String(40), nullable=True, index=True)  # resend, twilio, anthropic, netrows, hunter, apollo, internal
    # Idempotency key — meter() upserts on this. Lets retries (re-fired sequence
    # steps, webhook redeliveries, etc.) not double-charge.
    idempotency_key = Column(String(120), unique=True, index=True, nullable=False)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)


class AuditReportModel(Base):
    """Stored AI Findability Audit report for a company."""
    __tablename__ = "audit_reports"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, unique=True)
    token = Column(String(32), unique=True, index=True, nullable=False)
    html_content = Column(Text, nullable=False)

    # Scores for quick display without parsing HTML
    ai_findability_score = Column(Integer, default=0)
    content_citability_score = Column(Integer, default=0)
    local_seo_score = Column(Integer, default=0)
    overall_grade = Column(String(2), default="")
    findings_json = Column(Text, nullable=True)  # JSON array of top findings

    # Competitor comparison
    competitor_html = Column(Text, nullable=True)
    competitor_generated_at = Column(DateTime, nullable=True)

    # Tracking
    view_count = Column(Integer, default=0)
    last_viewed_at = Column(DateTime, nullable=True)
    generated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Set when prospect schedules via the iClosed widget on the gate page.
    # Source priority: iClosed webhook > prospect self-confirm via the
    # "I've Scheduled" button. nullable means: not booked yet.
    booked_at = Column(DateTime, nullable=True)
    booked_email = Column(String(255), nullable=True)


class SchedulingConfig(Base):
    """Per-user availability rules + display preferences for the native
    scheduler. One row per User; created lazily on first access.

    Recurring availability lives in `rules_json` as an array of:
      [{"weekday": 0-6 (Mon=0), "start_time": "09:00", "end_time": "12:00"}, ...]

    Date-specific overrides (vacation, special hours) intentionally
    deferred to a follow-up — recurring rules cover the 95% case.
    """
    __tablename__ = "scheduling_configs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)

    # Slot mechanics
    slot_minutes = Column(Integer, nullable=False, default=30)             # 15/30/45/60
    buffer_before_minutes = Column(Integer, nullable=False, default=0)
    buffer_after_minutes = Column(Integer, nullable=False, default=5)
    min_lead_time_hours = Column(Integer, nullable=False, default=4)        # don't show slots within X hours
    max_advance_days = Column(Integer, nullable=False, default=30)
    daily_limit = Column(Integer, nullable=False, default=0)                # 0 = unlimited

    # Recurring availability (JSON array)
    rules_json = Column(Text, nullable=True)

    # Booking page customization
    meeting_title = Column(String(120), nullable=False, default="Discovery Call")
    meeting_description = Column(Text, nullable=False,
                                 default="A quick call to walk through how Backyard Marketing Pros can grow your business.")
    page_headline = Column(String(200), nullable=False, default="Book a Discovery Call")
    page_intro = Column(Text, nullable=False,
                        default="Pick a time that works for you. The call lands on both our calendars and you'll get a confirmation email.")

    # Meeting location / conferencing.
    #   'google_meet'  → Google Calendar auto-generates a Meet link
    #   'phone'        → "Host will call you at <prospect phone>"
    #   'in_person'    → meeting_location_details is the address
    #   'custom_link'  → meeting_location_details is a Zoom/Teams URL or instructions
    meeting_type = Column(String(20), nullable=False, default="google_meet")
    meeting_location_details = Column(Text, nullable=True)

    # Custom intake questions on the booking form (iClosed-style).
    # Stored as JSON array of:
    #   {id: str, key: str, label: str, type: 'short_text'|'long_text'|'url'|'single_select',
    #    options: list[str] (single_select only), required: bool, position: int}
    # The built-in name/email/phone fields are always present and not in
    # this array. Answers persist on Booking.answers_json.
    booking_questions_json = Column(Text, nullable=True)

    # Extra Google calendar IDs to UNION into the free-busy check when
    # generating available slots. The user's primary + their write-target
    # calendar are always checked; this adds personal/family/work
    # calendars on top so a 2pm doctor appt blocks the 2pm-3pm slot.
    # Stored as a JSON array of calendar IDs.
    conflict_calendar_ids_json = Column(Text, nullable=True)

    # Public-booking-page brand customization. Hex strings (#RRGGBB).
    # `brand_color`        → buttons, slot-button selected/hover, radio
    #                        selected border + text, accent.
    # `accent_bg_color`    → soft tinted backgrounds (slot-row hover,
    #                        radio-option selected bg, header gradient
    #                        starts here).
    # `logo_url`           → optional. If set, renders above page-headline
    #                        in the booking-page header. Customer-supplied
    #                        URL — must be HTTPS for the image to load
    #                        cleanly inside our HTTPS page.
    brand_color = Column(String(20), nullable=False, default="#E65100")
    accent_bg_color = Column(String(20), nullable=False, default="#FFF8F0")
    logo_url = Column(String(500), nullable=True)

    # Public visibility — admin can deactivate without disconnecting Google
    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class Booking(Base):
    """A confirmed booking made through the native scheduler.

    Persisted independently of Google so we can rebuild state if the
    user disconnects, and so cancellation/reschedule UI doesn't depend
    on round-tripping the Google event id every time.

    `host_user_id` is the rep being booked. `prospect_*` fields capture
    what the booker submitted on the form. `google_event_id` is the
    event we created on the host's BMP Discovery Calls calendar."""
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True, index=True)
    host_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Time window (UTC)
    starts_at = Column(DateTime, nullable=False, index=True)
    ends_at = Column(DateTime, nullable=False)

    # Prospect-supplied
    prospect_name = Column(String(160), nullable=False)
    prospect_email = Column(String(255), nullable=False, index=True)
    prospect_phone = Column(String(40), nullable=True)
    prospect_message = Column(Text, nullable=True)
    prospect_timezone = Column(String(80), nullable=True)  # IANA tz, e.g. "America/Phoenix"

    # Linkage back to CRM (best-effort match on email at booking time)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)

    # Custom intake answers — JSON object keyed by question.key
    answers_json = Column(Text, nullable=True)

    # Google Calendar
    google_event_id = Column(String(255), nullable=True)
    google_event_link = Column(String(500), nullable=True)
    google_meet_link = Column(String(500), nullable=True)  # populated when meeting_type='google_meet'

    # Lifecycle
    status = Column(String(20), nullable=False, default="confirmed")  # confirmed/cancelled
    cancelled_at = Column(DateTime, nullable=True)
    cancelled_reason = Column(Text, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class Feedback(Base):
    """Team feedback / bug reports submitted via the in-app form."""
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    category = Column(String(40), nullable=False, default="feedback")  # feedback, bug, feature
    message = Column(Text, nullable=False)
    page = Column(String(80), nullable=True)  # which page they were on
    resolved = Column(Boolean, default=False)
    admin_notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class PendingDeletion(Base):
    """Soft-delete holding area. BDR deletions land here for admin approval."""
    __tablename__ = "pending_deletions"

    id = Column(Integer, primary_key=True, index=True)
    requested_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    entity_type = Column(String(20), nullable=False)  # company, contact, deal
    entity_id = Column(Integer, nullable=False)
    entity_name = Column(String(255), nullable=True)  # snapshot for display
    reason = Column(String(255), nullable=True)
    status = Column(String(20), nullable=False, default="pending")  # pending, approved, rejected
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
