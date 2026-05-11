"""
Observability layer — structured logs + Sentry.

Two halves:

1. **Structured logs**: every log record carries request_id as a
   first-class field. We inject it via a contextvar set by the
   request-id middleware. journalctl tail-ability is preserved —
   the format is human-readable `key=value` pairs, not JSON. So
   `journalctl -u backyard-leads | grep rid=abc123` just works.

2. **Sentry**: optional (controlled by SENTRY_DSN env var). When
   set, unhandled exceptions get captured with full request context
   (URL, method, headers, user_id), grouped by stack trace, and
   surfaced in the Sentry dashboard. Free tier covers 5k events/mo
   which is plenty for our volume.
"""
from __future__ import annotations
import contextvars
import logging
import os
import sys
from typing import Any

from app.config import settings

# Contextvar — set by RequestIdAndErrorHandler middleware per request.
# Defaults to "-" so logs from background tasks / startup are still
# readable instead of crashing on missing field.
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


class RequestIdFilter(logging.Filter):
    """Inject the current request_id contextvar onto every LogRecord
    so the formatter can include it. Always returns True (this is a
    filter that augments, not rejects)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


# Format: timestamp LEVEL logger rid=xxx message
# Compact enough to read in journalctl, structured enough to grep.
# Example:
#   2026-05-10T14:23:01Z INFO bmp.scheduler rid=eb2aa7e87e7c4caa booking created (id=42)
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s rid=%(request_id)s %(message)s"
_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%SZ"


def configure_logging() -> None:
    """Wire up the root logger with our format + filter. Called once
    at module import (from main.py). Safe to call multiple times —
    we clear handlers first to avoid duplicate output."""
    root = logging.getLogger()
    # Clear anything uvicorn/FastAPI may have attached
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    handler.addFilter(RequestIdFilter())
    root.addHandler(handler)

    # Default level. DEBUG locally if BMP_LOG_LEVEL is set; INFO in prod.
    level_name = os.environ.get("BMP_LOG_LEVEL", "INFO").upper()
    root.setLevel(getattr(logging, level_name, logging.INFO))

    # Quiet down noisy libraries — we don't want SQLAlchemy echo or
    # httpx's per-request INFO spam in production logs.
    for noisy in ("sqlalchemy.engine", "sqlalchemy.pool", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Uvicorn's access logger doesn't pick up our filter (different
    # logger hierarchy) — replace its formatter too so 'rid=' shows
    # up on every HTTP access line.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.propagate = True  # let our root handler do the work


def configure_sentry() -> bool:
    """Initialize Sentry SDK when SENTRY_DSN is set. Returns True if
    Sentry is now active, False if skipped. Failure to initialize
    Sentry never raises — observability is not allowed to break the
    app it's observing."""
    dsn = (os.environ.get("SENTRY_DSN") or getattr(settings, "sentry_dsn", "") or "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

        sentry_sdk.init(
            dsn=dsn,
            # 0.1 = sample 10% of requests for performance traces.
            # Set to 1.0 in dev / 0.0 to disable tracing entirely.
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            profiles_sample_rate=0.0,
            send_default_pii=False,  # Don't auto-include user emails / IPs
            release=os.environ.get("BMP_RELEASE", "dev"),
            environment=os.environ.get("BMP_ENV", "production"),
            integrations=[
                FastApiIntegration(transaction_style="endpoint"),
                StarletteIntegration(transaction_style="endpoint"),
                SqlalchemyIntegration(),
            ],
            # Scrub anything that looks like a token or secret out of
            # event payloads before they leave our process.
            before_send=_sentry_scrub,
        )
        logging.getLogger("bmp.observability").info(
            f"Sentry initialized (env={os.environ.get('BMP_ENV', 'production')})"
        )
        return True
    except Exception as e:
        # Don't let Sentry init failure crash the app
        logging.getLogger("bmp.observability").warning(
            f"Sentry init failed (continuing without it): {e}"
        )
        return False


def _sentry_scrub(event: dict, hint: dict) -> dict | None:
    """Strip request bodies, auth headers, and any value that looks
    like a key before sending to Sentry. Sentry's defaults already
    redact some — we belt-and-suspenders for our specific patterns."""
    try:
        # Drop the body of requests (may contain prospect data, notes,
        # email content) — we only need the URL + method for triage.
        req = event.get("request") or {}
        if "data" in req:
            req["data"] = "[redacted]"
        # Strip sensitive headers regardless of Sentry's defaults
        headers = req.get("headers") or {}
        for k in list(headers.keys()):
            if k.lower() in ("authorization", "cookie", "x-api-key", "x-webhook-signature"):
                headers[k] = "[redacted]"
    except Exception:
        pass
    return event


def set_request_context(*, request_id: str, user_id: int | None = None,
                        path: str | None = None) -> None:
    """Called by the request-id middleware on each request. Stamps
    the contextvar for logs + attaches the same info to the current
    Sentry scope so any error captured during this request includes
    it. No-ops cleanly if Sentry isn't configured."""
    request_id_ctx.set(request_id)
    try:
        import sentry_sdk
        scope = sentry_sdk.get_isolation_scope()
        scope.set_tag("request_id", request_id)
        if path:
            scope.set_tag("path", path)
        if user_id is not None:
            scope.set_user({"id": str(user_id)})
    except Exception:
        # Sentry not configured or already torn down — skip silently
        pass


def capture_exception(exc: BaseException, **extra: Any) -> None:
    """Manually report an exception to Sentry (e.g. from a background
    task that catches its own errors). No-op when Sentry isn't on."""
    try:
        import sentry_sdk
        if extra:
            with sentry_sdk.new_scope() as scope:
                for k, v in extra.items():
                    scope.set_extra(k, v)
                sentry_sdk.capture_exception(exc)
        else:
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass
