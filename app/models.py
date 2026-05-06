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
    role = Column(String(20), nullable=False, default="sales_rep")  # admin, sales_rep, read_only
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    searches = relationship("Search", back_populates="user")
    activities = relationship("Activity", back_populates="user")
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

    # BMP Package system
    package = Column(String(50), nullable=True)  # foundation, essential, growth, scale
    contract_months = Column(Integer, default=6)  # 6 or 12

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

    # Step type: email = auto-sendable, others = BDR task
    step_type = Column(String(20), default="email")  # email, linkedin, call, text, custom
    subject = Column(String(500), nullable=False)  # email subject, or task title for non-email
    body = Column(Text, nullable=False)  # email body, or LinkedIn message / call script / task notes
    email_type = Column(String(50), default="cold")  # cold, follow_up_1, follow_up_2, breakup, linkedin_connect, linkedin_message, call, text
    sequence_order = Column(Integer, default=1)
    send_delay_days = Column(Integer, default=0)
    scheduled_send_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)  # when email was sent or task was completed
    is_sent = Column(Boolean, default=False)  # for emails: actually sent. For tasks: completed by BDR.
    paused_at = Column(DateTime, nullable=True)
    problems_referenced = Column(Text)
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    company = relationship("Company", back_populates="activities")
    user = relationship("User", back_populates="activities")


class RuntimeConfig(Base):
    """Single-row org-level config that can be updated from the Settings UI.
    Keys here override env-var defaults (so users can rotate API keys without
    SSHing into the server).
    Always row id=1.
    """
    __tablename__ = "runtime_config"

    id = Column(Integer, primary_key=True, default=1)
    netrows_api_key = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


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
