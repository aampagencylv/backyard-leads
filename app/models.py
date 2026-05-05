from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)
    title = Column(String(255), default="")  # e.g. "Business Development Rep"
    phone = Column(String(50), default="")
    signature = Column(Text, default="")  # HTML email signature
    sending_enabled = Column(Boolean, default=False)  # Must be enabled per-user
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    searches = relationship("Search", back_populates="user")


class Search(Base):
    __tablename__ = "searches"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    keyword = Column(String(255), nullable=False)  # e.g. "pool builders"
    location = Column(String(255), nullable=False)  # e.g. "Austin, TX"
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
    business_type = Column(String(255))  # pool builder, landscaper, etc.

    # Enrichment data (from website crawl)
    enriched = Column(Boolean, default=False)
    site_speed_score = Column(Float)
    has_blog = Column(Boolean)
    has_social_links = Column(Boolean)
    last_review_date = Column(String(100))
    mobile_friendly = Column(Boolean)
    has_ssl = Column(Boolean)
    tech_stack = Column(Text)  # JSON string
    problems_found = Column(Text)  # JSON string of identified issues
    enrichment_summary = Column(Text)  # AI-generated summary of findings

    # Outreach status
    # new = just scraped, pursuing = selected for outreach, sequencing = emails being sent,
    # contacted = all emails sent, replied = prospect responded, qualified = confirmed lead,
    # converted = became customer, not_interested = opted out
    status = Column(String(50), default="new")
    email_generated = Column(Boolean, default=False)
    email_sent = Column(Boolean, default=False)
    pushed_to_hubspot = Column(Boolean, default=False)
    sequence_started_at = Column(DateTime, nullable=True)

    # Contact info (from enrichment)
    contact_name = Column(String(255))
    contact_email = Column(String(255))
    contact_title = Column(String(255))

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    search = relationship("Search", back_populates="leads")
    emails = relationship("GeneratedEmail", back_populates="lead")


class GeneratedEmail(Base):
    __tablename__ = "generated_emails"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    subject = Column(String(500), nullable=False)
    body = Column(Text, nullable=False)
    email_type = Column(String(50), default="cold")  # cold, follow_up_1, follow_up_2, breakup
    sequence_order = Column(Integer, default=1)  # 1=first email, 2=follow up 1, etc.
    send_delay_days = Column(Integer, default=0)  # days after sequence start to send
    scheduled_send_at = Column(DateTime, nullable=True)  # when this email should be sent
    sent_at = Column(DateTime, nullable=True)  # when it was actually sent
    is_sent = Column(Boolean, default=False)
    problems_referenced = Column(Text)  # which problems this email addresses
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    lead = relationship("Lead", back_populates="emails")
