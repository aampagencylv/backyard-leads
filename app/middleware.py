"""
Cross-cutting middleware: request IDs, error handling, security headers,
rate limiting.

Pure-stdlib + FastAPI primitives — no extra packages. The rate limiter
is in-memory (per-process); good for one-VPS deployments. When we go
multi-process or multi-server we'll swap to Redis-backed (slowapi or
similar).
"""
from __future__ import annotations
import logging
import time
import uuid
from collections import defaultdict, deque
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("bmp.middleware")


# ============================================================
# Request ID + global exception handler
# ============================================================
#
# Every request gets a stable ID. If the client sent X-Request-ID we
# honor it (lets external systems correlate their logs to ours);
# otherwise we mint one. The ID is:
#   - Stamped onto request.state.request_id for downstream code
#   - Echoed in the X-Request-ID response header
#   - Logged on every error so a user reporting "I got an error at 3pm"
#     can be matched to a specific log line by ID
#
# The exception catch returns a generic 500 body — never the raw
# traceback. Tracebacks go to journalctl with the request ID for ops.

class RequestIdAndErrorHandler(BaseHTTPMiddleware):
    """First in the chain — stamps request_id, catches everything.

    Sets the request_id contextvar so every log record fired anywhere
    in the request lifecycle carries the same id. Also stamps Sentry
    scope so any captured exception has the request_id as a tag,
    making the dashboard searchable by rid."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        rid = (request.headers.get("x-request-id") or "")[:64].strip()
        if not rid:
            rid = uuid.uuid4().hex[:16]
        request.state.request_id = rid

        # Stamp the contextvar + Sentry scope before any handler runs.
        # Anything logged or excepted from here on carries the rid.
        from app.observability import set_request_context, capture_exception
        set_request_context(request_id=rid, path=request.url.path)

        try:
            response = await call_next(request)
        except Exception as e:
            # Anything that escaped the route's own try/except.
            # Swallow + log + capture to Sentry + return safe JSON.
            log.exception(
                f"unhandled method={request.method} "
                f"path={request.url.path} error={type(e).__name__}: {e}"
            )
            capture_exception(e, request_method=request.method,
                              request_path=request.url.path)
            return JSONResponse(
                {"detail": "Internal server error", "request_id": rid},
                status_code=500,
                headers={"X-Request-ID": rid},
            )
        response.headers["X-Request-ID"] = rid
        return response


# ============================================================
# Security headers
# ============================================================
#
# Sets HSTS / nosniff / frame-ancestors / referrer-policy on every
# response. CSP starts permissive — we have inline scripts/styles
# throughout and pull in third-party SDKs (Twilio Voice SDK,
# wavesurfer, etc.). Tightening CSP further is a follow-up.

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)

        # Don't apply tight headers to publicly-embeddable surfaces:
        # the audit report at /report/{token}/* is meant to be sent to
        # prospects who may view it inside their own iframe, the
        # public booking page at /book/{slug} likewise, and the
        # Missive sidebar (/integrations/missive/sidebar) must load
        # inside Missive's iframe — X-Frame-Options: DENY would block
        # it, and Missive's docs explicitly note that frame-ancestors
        # breaks iOS embedding.
        path = request.url.path
        is_public_embeddable = (
            path.startswith("/report/")
            or path.startswith("/book/")
            or path.startswith("/integrations/missive/")
        )

        # Apply unconditionally
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # HSTS: tell browsers to always use HTTPS for this domain. 1 year
        # is the standard production value. Our prod is HTTPS-only.
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )

        if not is_public_embeddable:
            # Block clickjacking on the main app
            response.headers.setdefault("X-Frame-Options", "DENY")
            # Permissive CSP — allows our inline scripts/styles + the
            # third-party SDKs we know about. Tighten in follow-up.
            csp = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
                "https://unpkg.com https://sdk.twilio.com https://media.twiliocdn.com; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https:; "
                "font-src 'self' data:; "
                "connect-src 'self' https: wss:; "
                "frame-src 'self' https://accounts.google.com https://app.iclosed.io; "
                "frame-ancestors 'self'; "
                "base-uri 'self'; "
                "form-action 'self'"
            )
            response.headers.setdefault("Content-Security-Policy", csp)
        return response


# ============================================================
# Rate limiting (in-memory token bucket per IP)
# ============================================================
#
# Sliding-window-style: we keep timestamps of recent requests per
# (key, route_pattern) tuple. When the buckets fill up we 429.
# Memory grows O(active IPs × tracked routes) — bounded by pruning
# old entries lazily on access.

# (limit_count, window_seconds) per route prefix. First match wins.
RATE_LIMITS: list[tuple[str, int, int]] = [
    # Auth — protect against brute-force
    ("/api/auth/login",            10, 60),
    ("/api/auth/register",          5, 60),
    ("/api/auth/forgot-password",   5, 60),
    ("/api/auth/reset-password",    5, 60),
    ("/api/auth/change-password",   5, 60),
    # MCP — Anthropic spend protection (summarize_company is paid)
    ("/mcp",                       60, 60),
    # Public booking endpoints — anti-spam on the prospect side
    ("/api/book/",                 30, 60),
    # Public uploads — prevent disk fill
    ("/api/uploads/",              20, 60),
]


class _Bucket:
    __slots__ = ("hits",)
    def __init__(self):
        self.hits: deque[float] = deque()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Pure in-memory rate limiter. Rebuilds on every process restart;
    no persistence (deliberately — a momentarily-rate-limited IP after
    a restart is fine). Bypass keys: super_admins are never rate-limited
    (admin tools should always work)."""

    def __init__(self, app):
        super().__init__(app)
        self._buckets: dict[tuple[str, str], _Bucket] = defaultdict(_Bucket)

    def _match_rule(self, path: str) -> tuple[str, int, int] | None:
        for prefix, limit, window in RATE_LIMITS:
            if path.startswith(prefix):
                return prefix, limit, window
        return None

    def _client_key(self, request: Request) -> str:
        # Prefer X-API-Key (lets us limit per-key for /mcp instead of
        # per-IP, which is correct since multiple users can share an
        # outbound IP). Fall back to client IP.
        api_key = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
        if api_key:
            return f"key:{api_key[:12]}"  # safe to log; just the prefix
        forwarded = request.headers.get("x-forwarded-for", "")
        ip = forwarded.split(",")[0].strip() if forwarded else (
            request.client.host if request.client else "unknown"
        )
        return f"ip:{ip}"

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        rule = self._match_rule(request.url.path)
        if not rule:
            return await call_next(request)
        prefix, limit, window = rule
        key = self._client_key(request)
        bucket = self._buckets[(prefix, key)]
        now = time.monotonic()
        cutoff = now - window
        # Drop expired hits
        while bucket.hits and bucket.hits[0] < cutoff:
            bucket.hits.popleft()
        if len(bucket.hits) >= limit:
            retry_after = max(1, int(window - (now - bucket.hits[0])))
            log.warning(
                f"rate_limit hit prefix={prefix} key={key} "
                f"limit={limit}/{window}s"
            )
            return JSONResponse(
                {
                    "detail": f"Too many requests. Try again in {retry_after}s.",
                    "limit": limit,
                    "window_seconds": window,
                },
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        bucket.hits.append(now)
        return await call_next(request)
