from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    app_name: str = "Backyard Leads"
    secret_key: str = "change-me-in-production"
    database_url: str = "sqlite+aiosqlite:///./leads.db"
    anthropic_api_key: str = ""
    google_maps_api_key: str = ""
    apollo_api_key: str = ""
    hunter_api_key: str = ""
    resend_api_key: str = ""
    resend_webhook_secret: str = ""
    send_domain: str = "go.backyardmarketingpros.com"
    reply_domain: str = "backyardmarketingpros.com"
    public_url: str = "https://prospector.backyardmarketingpros.com"
    bmp_postal_address: str = "Backyard Marketing Pros, Las Vegas, NV"  # CAN-SPAM requires a real postal address; override in .env

    class Config:
        env_file = ".env"


@lru_cache
def get_settings():
    return Settings()


settings = get_settings()
