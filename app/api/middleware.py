"""HTTP middleware: request correlation + access log, body-size limit, security headers, rate limiting.

Pure ASGI-style `BaseHTTPMiddleware` classes kept small and independent.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.logging_config import request_id_var, session_id_var

logger = logging.getLogger("app.access")

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_SESSION_IN_PATH = re.compile(r"^/sessions/([0-9a-f]{32})(?:/|$)")


def client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id (or accepts a well-formed X-Request-ID), tags logs with the
    session id from the URL, and writes one access-log line per request."""

    def __init__(self, app, log_requests: bool = True) -> None:
        super().__init__(app)
        self.log_requests = log_requests

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex[:16]
        m = _SESSION_IN_PATH.match(request.url.path)
        t_req = request_id_var.set(rid)
        t_sess = session_id_var.set(m.group(1)[:8] if m else "-")
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            if self.log_requests and request.url.path not in ("/health/live",):
                logger.info("%s %s -> %d %.0fms", request.method, request.url.path, status,
                            (time.perf_counter() - start) * 1000)
            request_id_var.reset(t_req)
            session_id_var.reset(t_sess)


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > self.max_bytes:
            return JSONResponse(status_code=413, content={"detail": "Request is too large."})
        if request.method in ("POST", "PUT", "PATCH") and not length:
            body = await request.body()  # chunked upload without a length: measure it
            if len(body) > self.max_bytes:
                return JSONResponse(status_code=413, content={"detail": "Request is too large."})
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith("/widget"):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors *; base-uri 'none'; form-action 'self'")
        elif not request.url.path.startswith(("/docs", "/redoc")):
            response.headers.setdefault("X-Frame-Options", "DENY")
        return response


class SlidingWindowLimiter:
    """In-memory sliding-window counter per key (single process)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_s: float, now: float | None = None) -> tuple[bool, int]:
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > window_s:
                q.popleft()
            if len(q) >= limit:
                retry = int(window_s - (now - q[0])) + 1
                return False, retry
            q.append(now)
            if len(self._hits) > 50_000:  # bound memory: drop idle keys
                for k in [k for k, v in self._hits.items() if not v][:10_000]:
                    del self._hits[k]
            return True, 0


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP limits: general API calls per minute, and new sessions per hour.

    Health checks, docs and static widget files are not limited.
    """

    EXEMPT = ("/health", "/docs", "/redoc", "/openapi.json", "/widget", "/favicon.ico")

    def __init__(self, app, settings, limiter: SlidingWindowLimiter | None = None) -> None:
        super().__init__(app)
        self.settings = settings
        self.limiter = limiter or SlidingWindowLimiter()

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not self.settings.rate_limit_enabled or path.startswith(self.EXEMPT):
            return await call_next(request)
        ip = client_ip(request, self.settings.trust_proxy_headers)
        ok, retry = self.limiter.allow(f"api:{ip}", self.settings.rate_limit_per_minute, 60)
        if ok and request.method == "POST" and path.rstrip("/") == "/sessions":
            ok, retry = self.limiter.allow(f"sess:{ip}", self.settings.rate_limit_sessions_per_hour, 3600)
        if not ok:
            logger.warning("rate limit hit ip=%s path=%s", ip, path)
            return JSONResponse(status_code=429, headers={"Retry-After": str(retry)},
                                content={"detail": "Too many requests. Please wait a moment and try again."})
        return await call_next(request)
