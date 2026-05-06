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

    # Lead source / classification
    linkedin_url = Column(String(500), nullable=True)

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
    """A revenue opportunity at a Company. Multiple deals per company supported (over time, or in parallel)."""
    __tablename__ = "deals"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)

    name = Column(String(255), nullable=False)
    value = Column(Float, nullable=True)  # monthly retainer or one-time
    stage = Column(String(50), default="prospecting")  # prospecting, qualified, proposal, negotiation, closed_won, closed_lost
    pipeline = Column(String(50), default="default")
    probability = Column(Integer, default=0)  # 0-100, used for forecast
    expected_close_date = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    lost_reason = Column(String(255), nullable=True)

    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    company = relationship("Company", back_populates="deals")
    assigned_user = relationship("User", foreign_keys=[assigned_to])


class GeneratedEmail(Base):
    __tablename__ = "generated_emails"

    id = Column(Integer, primary_key=True, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)  # denormalized for fast company timeline queries

    subject = Column(String(500), nullable=False)
    body = Column(Text, nullable=False)
    email_type = Column(String(50), default="cold")
    sequence_order = Column(Integer, default=1)
    send_delay_days = Column(Integer, default=0)
    scheduled_send_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    is_sent = Column(Boolean, default=False)
    paused_at = Column(DateTime, nullable=True)  # set when sequence is paused (e.g. on reply)
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
