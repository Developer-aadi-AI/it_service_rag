"""Chat sessions: create, send messages, poll, preferences, close."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from app.api.deps import get_services, message_out, notification_for, session_state, sources_out
from app.api.schemas import (ChatDebug, ChatRequest, ChatResponse, MessagesResponse, PreferencesUpdate,
                             SessionCreate, SessionCreated, SessionState)
from app.services import Services

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _chat_response(svc: Services, result) -> ChatResponse:
    debug = None
    if svc.settings.environment != "production":
        rag = result.rag
        debug = ChatDebug(sentiment=result.sentiment.label if result.sentiment else None,
                          search_query=rag.search_query if rag else None,
                          top_score=rag.top_score if rag else None, generator=rag.generator if rag else None)
    return ChatResponse(
        session=session_state(result.session, svc.settings), message=message_out(result.reply),
        status=result.status, sources=sources_out(result.rag), recommendations=result.recommendations,
        action=result.action, notification=notification_for(result.session, [result.reply]), debug=debug)


@router.post("", response_model=SessionCreated, status_code=201)
def create_session(body: SessionCreate | None = None, svc: Services = Depends(get_services)) -> SessionCreated:
    """Start a chat. The greeting message is returned immediately."""
    body = body or SessionCreate()
    result = svc.manager.start_session(body.name, str(body.email) if body.email else None, body.sound_enabled)
    return SessionCreated(session=session_state(result.session, svc.settings), message=message_out(result.reply))


@router.get("/{session_id}", response_model=SessionState)
def get_session(session_id: str, svc: Services = Depends(get_services)) -> SessionState:
    return session_state(svc.manager.get(session_id), svc.settings)


@router.post("/{session_id}/messages", response_model=ChatResponse)
async def send_message(session_id: str, body: ChatRequest, svc: Services = Depends(get_services)) -> ChatResponse:
    """Send a user message and get the assistant's reply (grounded answer, flows, recommendations)."""
    try:
        result = await run_in_threadpool(svc.manager.handle_message, session_id, body.message)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _chat_response(svc, result)


@router.get("/{session_id}/messages", response_model=MessagesResponse)
def list_messages(session_id: str, after: int = Query(0, ge=0, description="return messages with seq > after"),
                  svc: Services = Depends(get_services)) -> MessagesResponse:
    """Poll for new messages, e.g. the idle check or the inactivity close message."""
    session, messages = svc.manager.messages_after(session_id, after)
    return MessagesResponse(session=session_state(session, svc.settings),
                            messages=[message_out(m) for m in messages],
                            notification=notification_for(session, messages if after else []))


@router.patch("/{session_id}/preferences", response_model=SessionState)
def update_preferences(session_id: str, body: PreferencesUpdate, svc: Services = Depends(get_services)) -> SessionState:
    """Enable/disable notification sounds for this chat."""
    return session_state(svc.manager.set_preferences(session_id, body.sound_enabled), svc.settings)


@router.post("/{session_id}/close", response_model=ChatResponse)
def close_session(session_id: str, svc: Services = Depends(get_services)) -> ChatResponse:
    """End the chat (e.g. the user closed the widget). Idempotent."""
    return _chat_response(svc, svc.manager.close_by_user(session_id))
