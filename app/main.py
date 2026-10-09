"""FastAPI application entry point.

Run (dev):   uvicorn app.main:app --reload
Run (prod):  see docs/DEPLOYMENT.md (single worker: chat sessions live in process memory)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.api import admin, feedback, leads, routes, sessions, transcripts
from app.api.middleware import (BodySizeLimitMiddleware, RateLimitMiddleware, RequestContextMiddleware,
                                SecurityHeadersMiddleware)
from app.config import PROJECT_ROOT, get_settings
from app.conversation.idle import IdleMonitor
from app.conversation.store import SessionClosed, SessionLimitReached, SessionNotFound
from app.feedback.service import InvalidFeedback
from app.leads.service import LeadCaptureError
from app.logging_config import register_secrets, setup_logging
from app.rag.pipeline import build_pipeline
from app.services import build_services

logger = logging.getLogger(__name__)
WIDGET_DIR = PROJECT_ROOT / "app" / "static" / "widget"

TAGS = [
    {"name": "chat", "description": "Stateless grounded answers (`/ask`)."},
    {"name": "sessions", "description": "Multi-turn chat sessions: memory, follow-ups, flows, idle handling."},
    {"name": "leads", "description": "Contact/support form submissions (validated, stored in SQLite)."},
    {"name": "feedback", "description": "Great / OK / Poor ratings from the chat or signed email links."},
    {"name": "transcripts & email", "description": "Transcript download and customer emails."},
    {"name": "admin", "description": "Analytics (requires `X-Admin-Key`; disabled unless ADMIN_API_KEY is set)."},
    {"name": "system", "description": "Health, liveness and readiness probes."},
]


def _register_secrets(settings) -> None:
    register_secrets(settings.anthropic_api_key, settings.groq_api_key, settings.openai_api_key,
                     settings.gemini_api_key, settings.smtp_password, settings.app_secret_key, settings.admin_api_key)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)
    _register_secrets(settings)
    if getattr(app.state, "services", None) is None:  # tests may inject services
        bundle = getattr(app.state, "bundle", None)
        if bundle is None:
            logger.info("building RAG pipeline...")
            bundle = build_pipeline(settings)
        app.state.services = build_services(settings, bundle)
        logger.info("ready: env=%s mode=%s chunks=%d email=%s", settings.environment, bundle.pipeline.mode,
                    bundle.store.count(), settings.email_backend)
    services = app.state.services
    monitor = None
    if services.settings.idle_monitor_enabled:
        monitor = IdleMonitor(services.manager, services.settings.idle_sweep_interval_seconds)
        monitor.start()
    try:
        yield
    finally:
        if monitor:
            await monitor.stop()
        if getattr(app.state, "owns_services", True):
            services.shutdown()


def _json_error(status: int, detail: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, **extra})


def create_app(settings=None) -> FastAPI:
    """Build the app. `settings` overrides the environment (used by tests)."""
    settings = settings or get_settings()
    production = settings.environment == "production"
    app = FastAPI(
        title=settings.app_name, version="3.0.0", lifespan=lifespan, openapi_tags=TAGS,
        description=("D Group AI customer-support and sales assistant: grounded RAG answers with sources, "
                     "multi-turn chat sessions, recommendations, lead capture, transcripts, email and analytics. "
                     "See docs/API.md for the full reference."),
        docs_url=None if production else "/docs", redoc_url=None if production else "/redoc",
    )

    # Middleware (last added = outermost): context/logging -> security headers -> size limit -> rate limit -> CORS
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=False,
                       allow_methods=["GET", "POST", "PATCH"], allow_headers=["Content-Type", "X-Request-ID", "X-Admin-Key"],
                       expose_headers=["X-Request-ID", "Retry-After"])
    app.add_middleware(RateLimitMiddleware, settings=settings)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware, log_requests=settings.log_requests)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errors = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        return _json_error(422, "Invalid request", errors=errors)

    @app.exception_handler(ValidationError)
    async def _model_validation(request: Request, exc: ValidationError):
        errors = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
        return _json_error(422, "Invalid request", errors=errors)

    @app.exception_handler(SessionNotFound)
    async def _not_found(request: Request, exc: SessionNotFound):
        return _json_error(404, "Chat session not found. Please start a new chat.")

    @app.exception_handler(SessionClosed)
    async def _closed(request: Request, exc: SessionClosed):
        return _json_error(409, "This chat has ended. Please start a new chat.")

    @app.exception_handler(SessionLimitReached)
    async def _busy(request: Request, exc: SessionLimitReached):
        return JSONResponse(status_code=503, headers={"Retry-After": "60"},
                            content={"detail": "The assistant is very busy right now. Please try again shortly."})

    @app.exception_handler(LeadCaptureError)
    async def _lead_failed(request: Request, exc: LeadCaptureError):
        return _json_error(503, "We couldn't save your details right now. Please try again in a moment.")

    @app.exception_handler(InvalidFeedback)
    async def _bad_feedback(request: Request, exc: InvalidFeedback):
        return _json_error(422, str(exc))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        services = getattr(request.app.state, "services", None)
        if services is not None:
            services.events.error("api", exc)
        return _json_error(500, "Something went wrong. Please try again.")

    for module in (routes, sessions, leads, feedback, transcripts, admin):
        app.include_router(module.router)

    app.mount("/widget", StaticFiles(directory=WIDGET_DIR, html=True), name="widget")

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/widget/")

    return app


app = create_app()
