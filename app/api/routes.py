"""Health checks and the stateless /ask endpoint (Phase 1 contract kept)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.api.schemas import AskRequest, AskResponse, DebugInfo, ErrorResponse, HealthResponse, SourceOut
from app.config import Settings, get_settings
from app.rag.pipeline import PipelineBundle

logger = logging.getLogger(__name__)
router = APIRouter()


def get_bundle(request: Request) -> PipelineBundle:
    services = getattr(request.app.state, "services", None)
    bundle = services.bundle if services is not None else getattr(request.app.state, "bundle", None)
    if bundle is None:
        raise HTTPException(status_code=503, detail="Assistant is starting up, please retry shortly.")
    return bundle


def _checks(request: Request, bundle: PipelineBundle) -> tuple[dict[str, str], int | None]:
    checks = {"vector_index": "ok" if bundle.store.count() > 0 else "error"}
    services = getattr(request.app.state, "services", None)
    active = None
    if services is not None:
        try:
            services.db.query("SELECT 1")
            checks["database"] = "ok"
        except Exception:
            checks["database"] = "error"
        checks["email"] = "disabled" if not services.emails.enabled else services.emails.sender.name
        checks["analytics"] = "ok" if services.settings.analytics_enabled else "disabled"
        active = services.store.active_count()
    checks["llm"] = bundle.pipeline.mode
    return checks, active


@router.get("/health", response_model=HealthResponse, tags=["system"], summary="Status and component checks")
def health(request: Request, bundle: PipelineBundle = Depends(get_bundle), settings: Settings = Depends(get_settings)):
    checks, active = _checks(request, bundle)
    status = "ok" if all(v != "error" for v in checks.values()) else "degraded"
    return HealthResponse(
        status=status, environment=settings.environment, mode=bundle.pipeline.mode,
        documents=bundle.index_report.documents, chunks=bundle.store.count(),
        similarity_threshold=settings.similarity_threshold, domain_threshold=settings.domain_threshold,
        top_k=settings.top_k, checks=checks, active_sessions=active)


@router.get("/health/live", tags=["system"], summary="Liveness probe (process is up)")
def live() -> dict:
    return {"status": "ok"}


@router.get("/health/ready", tags=["system"], summary="Readiness probe (index + database usable)",
            responses={503: {"model": ErrorResponse}})
def ready(request: Request):
    services = getattr(request.app.state, "services", None)
    if services is None:
        return JSONResponse(status_code=503, content={"detail": "starting"})
    checks, _ = _checks(request, services.bundle)
    if any(v == "error" for v in checks.values()):
        return JSONResponse(status_code=503, content={"detail": "not ready", "checks": checks})
    return {"status": "ready", "checks": checks}


@router.post("/ask", response_model=AskResponse, tags=["chat"], summary="Single grounded answer (stateless)",
             responses={422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}})
async def ask(body: AskRequest, request: Request, bundle: PipelineBundle = Depends(get_bundle),
              settings: Settings = Depends(get_settings)) -> AskResponse:
    """Stateless single-question endpoint wrapping the core RAG pipeline.

    For multi-turn chat with memory, use the /sessions endpoints.
    """
    try:
        # Embedding + LLM calls are blocking; keep the event loop free.
        result = await run_in_threadpool(bundle.pipeline.answer, body.question, None,
                                         body.top_k, body.similarity_threshold)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    services = getattr(request.app.state, "services", None)
    if services is not None:
        services.events.record("ask", None, status=result.status, latency_ms=result.latency_ms,
                               top_score=result.top_score, generator=result.generator, question=body.question)
    debug = None
    if settings.environment != "production":
        debug = DebugInfo(top_score=result.top_score, generator=result.generator,
                          mode=bundle.pipeline.mode, latency_ms=result.latency_ms,
                          source_scores={s.number: s.score for s in result.sources})
    return AskResponse(
        request_id=result.request_id, answer=result.answer, status=result.status,
        sources=[SourceOut(number=s.number, title=s.title, url=s.url, source_type=s.source_type,
                           platform=s.platform, category=s.category, snippet=s.snippet)
                 for s in result.sources],
        action=result.action, off_topic=result.off_topic, request_kind=result.request_kind, debug=debug,
    )
