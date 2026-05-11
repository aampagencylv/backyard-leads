from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    app_name: str = "Backyard Leads"
    secret_key: str = "change-me-in-production"
    database_url: str = "sqlite+aiosqlite:///./leads.db"
    anthropic_api_key: str = ""
    google_maps_api_key: str = ""
    hunter_api_key: str = ""
    netrows_api_key: str = ""
    dataforseo_login: str = ""
    dataforseo_password: str = ""
    iclosed_api_key: str = ""  # ⚠️ Expires May 2027 — rotate annually
    iclosed_booking_url: str = "https://app.iclosed.io/e/backyardmarketingpros/discovery-call"
    # Shared-secret token appended to the iClosed webhook URL as ?t=<secret>.
    # Without this set, the webhook is open to anyone who guesses the URL.
    # When set, requests without ?t=<matching value> get a 401. Configure
    # in iClosed by setting the webhook URL to:
    #   https://prospector.backyardmarketingpros.com/api/iclosed/webhook?t=<secret>
    iclosed_webhook_secret: str = ""
    resend_api_key: str = ""
    resend_webhook_secret: str = ""
    send_domain: str = "go.backyardmarketingpros.com"
    reply_domain: str = "backyardmarketingpros.com"
    # Reply-To token routing — Reply-To becomes `r-<token>@<inbound_reply_domain>`.
    # Resend Inbound catches every email at this domain via catch-all and POSTs to
    # /api/email/inbound. We use the same domain as outbound (go.bymp.com already
    # has Receiving enabled in Resend with a verified MX) — no separate
    # subdomain needed. The `r-<27char>` local-part won't collide with normal
    # user addresses since no real person has a username that long + random.
    inbound_reply_domain: str = "go.backyardmarketingpros.com"
    public_url: str = "https://prospector.backyardmarketingpros.com"
    # Audit reports live on their own subdomain so links in cold emails
    # don't expose the internal CRM hostname. Same backend serves both —
    # Nginx routes audit.backyardmarketingpros.com to the same FastAPI
    # app, and /report/* / /book/* paths respond identically on both
    # hostnames. Override in .env if your audit subdomain differs.
    audit_public_url: str = "https://audit.backyardmarketingpros.com"
    # Booking pages (native scheduler /book/{slug}) live on their own
    # subdomain. Same rationale as audit: prospect-facing surface that
    # shouldn't leak the internal CRM hostname, and a clean CNAME target
    # for white-labeled SaaS tenants down the road.
    schedule_public_url: str = "https://schedule.backyardmarketingpros.com"
    bmp_postal_address: str = "Backyard Marketing Pros, Las Vegas, NV"  # CAN-SPAM requires a real postal address; override in .env

    # Google OAuth — per-user Google Calendar integration for the native
    # scheduler. Register a Web app in Google Cloud Console; authorized
    # redirect URI must match `<public_url>/api/google/oauth/callback`.
    # Required scopes (set on the consent screen):
    #   - openid
    #   - https://www.googleapis.com/auth/userinfo.email
    #   - https://www.googleapis.com/auth/calendar.readonly
    #   - https://www.googleapis.com/auth/calendar.events
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""

    # Sentry DSN — optional. Leave empty to disable error monitoring.
    # When set, unhandled exceptions get captured with full request
    # context. Free tier handles 5k events/month.
    sentry_dsn: str = ""

    class Config:
        env_file = ".env"


@lru_cache
def get_settings():
    return Settings()


settings = get_settings()
