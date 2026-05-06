from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey, Boolean, Table
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from app.database import Base


# Many-to-many for leads <-> tags
lead_tags = Table(
    "lead_tags",
    Base.metadata,
    Column("lead_id", Integer, ForeignKey("leads.id"), primary_key=True),
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
    leads = relationship("Lead", back_populates="search")


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    search_id = Column(Integer, ForeignKey("searches.id"), nullable=True)

    # Basic info from map scraping
    business_name = Column(String(500), nullable=False)
    phone = Column(String(50))
    website = Column(String(500))
    address = Column(String(500))
    city = Column(String(255))
    state = Column(String(100))
    rating = Column(Float)
    review_count = Column(Integer)
    business_type = Column(String(255))

    # Enrichment data
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

    # CRM fields
    status = Column(String(50), default="new")
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    deal_value = Column(Float, nullable=True)  # Monthly retainer value
    deal_stage = Column(String(50), default="prospect")  # prospect, proposal, negotiation, closed_won, closed_lost
    linkedin_url = Column(String(500), nullable=True)

    # Outreach
    email_generated = Column(Boolean, default=False)
    email_sent = Column(Boolean, default=False)
    pushed_to_hubspot = Column(Boolean, default=False)
    sequence_started_at = Column(DateTime, nullable=True)

    # Contact info
    contact_name = Column(String(255))
    contact_email = Column(String(255))
    contact_title = Column(String(255))
    contact_phone = Column(String(50))
    contact_linkedin = Column(String(500))

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    search = relationship("Search", back_populates="leads")
    emails = relationship("GeneratedEmail", back_populates="lead", order_by="GeneratedEmail.sequence_order")
    activities = relationship("Activity", back_populates="lead", order_by="Activity.created_at.desc()")
    tasks = relationship("Task", back_populates="lead", order_by="Task.due_date")
    tags = relationship("Tag", secondary=lead_tags, back_populates="leads")
    assigned_user = relationship("User", foreign_keys=[assigned_to])


class GeneratedEmail(Base):
    __tablename__ = "generated_emails"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    subject = Column(String(500), nullable=False)
    body = Column(Text, nullable=False)
    email_type = Column(String(50), default="cold")
    sequence_order = Column(Integer, default=1)
    send_delay_days = Column(Integer, default=0)
    scheduled_send_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    is_sent = Column(Boolean, default=False)
    problems_referenced = Column(Text)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    lead = relationship("Lead", back_populates="emails")


class Activity(Base):
    """Timeline of everything that happens with a lead."""
    __tablename__ = "activities"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Types: note, call, email_sent, email_opened, email_clicked, email_bounced,
    #        status_change, enriched, meeting, linkedin_message, task_completed
    activity_type = Column(String(50), nullable=False)
    content = Column(Text, default="")  # Note text or auto description
    metadata_json = Column(Text, nullable=True)  # Extra data as JSON
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    lead = relationship("Lead", back_populates="activities")
    user = relationship("User", back_populates="activities")


class Tag(Base):
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    color = Column(String(20), default="#1B5E20")  # BMP green default

    leads = relationship("Lead", secondary=lead_tags, back_populates="tags")


class Task(Base):
    """Follow-up reminders and to-dos for leads."""
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    description = Column(String(500), nullable=False)
    due_date = Column(DateTime, nullable=True)
    completed = Column(Boolean, default=False)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    lead = relationship("Lead", back_populates="tasks")
    user = relationship("User", back_populates="tasks")
