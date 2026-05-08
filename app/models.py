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

    # Twilio Voice — per-rep phone number + SDK identity
    twilio_phone_number = Column(String(40), nullable=True)  # E.164 format, e.g. +17025551234
    twilio_identity = Column(String(80), nullable=True)      # SDK identity, e.g. "bmp_user_3"

    # Per-rep dial preferences
    # 'browser' = WebRTC via Twilio.Device (default; needs headset + good internet)
    # 'bridge'  = CallRail-style: Twilio rings personal_phone_number first, bridges to prospect
    dial_mode = Column(String(20), nullable=False, default="browser")
    personal_phone_number = Column(String(40), nullable=True)  # E.164; required when dial_mode='bridge'

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

    # Google Maps reviews cache (Netrows /google-maps/reviews)
    google_place_id = Column(String(80), nullable=True)
    reviews_json = Column(Text, nullable=True)  # JSON array of reviews with owner_reply parsed out
    reviews_fetched_at = Column(DateTime, nullable=True)

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

    # Personalization context cache (Netrows /people/posts)
    recent_posts_json = Column(Text, nullable=True)  # JSON array of {text, posted_at, url, likes}
    posts_fetched_at = Column(DateTime, nullable=True)

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

    # Call review / coaching feedback (admin rates BDR calls)
    call_rating = Column(Integer, nullable=True)  # 1-5 stars
    call_feedback = Column(Text, nullable=True)  # Admin written feedback
    rated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    rated_at = Column(DateTime, nullable=True)

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
