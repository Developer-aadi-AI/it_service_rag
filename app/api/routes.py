"""Phase 1 routes: health and the stateless /ask endpoint (unchanged contract)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.api.schemas import AskRequest, AskResponse, DebugInfo, HealthResponse, SourceOut
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


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health(bundle: PipelineBundle = Depends(get_bundle), settings: Settings = Depends(get_settings)):
    return HealthResponse(
        status="ok", mode=bundle.pipeline.mode, documents=bundle.index_report.documents,
        chunks=bundle.store.count(), similarity_threshold=settings.similarity_threshold,
        domain_threshold=settings.domain_threshold, top_k=settings.top_k,
    )


@router.post("/ask", response_model=AskResponse, tags=["chat"])
async def ask(body: AskRequest, bundle: PipelineBundle = Depends(get_bundle),
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
