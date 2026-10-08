"""FastAPI application entry point.

Run:  uvicorn app.main:app --reload
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api import feedback, leads, routes, sessions, transcripts
from app.config import get_settings
from app.conversation.idle import IdleMonitor
from app.conversation.store import SessionClosed, SessionNotFound
from app.feedback.service import InvalidFeedback
from app.logging_config import setup_logging
from app.rag.pipeline import build_pipeline
from app.services import build_services

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    if getattr(app.state, "services", None) is None:  # tests may inject services
        bundle = getattr(app.state, "bundle", None)
        if bundle is None:
            logger.info("building RAG pipeline...")
            bundle = build_pipeline(settings)
        app.state.services = build_services(settings, bundle)
        logger.info("ready: %s, %d chunks", bundle.pipeline.mode, bundle.store.count())
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


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="2.0.0", lifespan=lifespan,
                  description="V Group AI customer-support and sales assistant: grounded RAG answers, "
                              "multi-turn chat sessions, recommendations, lead capture, transcripts and email.")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list,
                       allow_methods=["GET", "POST", "PATCH"], allow_headers=["*"])

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errors = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": "Invalid request", "errors": errors})

    @app.exception_handler(ValidationError)
    async def _model_validation(request: Request, exc: ValidationError):
        errors = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": "Invalid request", "errors": errors})

    @app.exception_handler(SessionNotFound)
    async def _not_found(request: Request, exc: SessionNotFound):
        return JSONResponse(status_code=404, content={"detail": "Chat session not found. Please start a new chat."})

    @app.exception_handler(SessionClosed)
    async def _closed(request: Request, exc: SessionClosed):
        return JSONResponse(status_code=409, content={"detail": "This chat has ended. Please start a new chat."})

    @app.exception_handler(InvalidFeedback)
    async def _bad_feedback(request: Request, exc: InvalidFeedback):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Something went wrong. Please try again."})

    for module in (routes, sessions, leads, feedback, transcripts):
        app.include_router(module.router)
    return app


app = create_app()
