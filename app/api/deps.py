"""Shared API helpers: service access and response mapping."""
from __future__ import annotations

from fastapi import HTTPException, Request

from app.api.schemas import MessageOut, Notification, SessionState, SourceOut
from app.conversation.models import Message, Session
from app.services import Services


def get_services(request: Request) -> Services:
    services = getattr(request.app.state, "services", None)
    if services is None:
        raise HTTPException(status_code=503, detail="Assistant is starting up, please retry shortly.")
    return services


def session_state(session: Session, settings) -> SessionState:
    return SessionState(
        session_id=session.session_id, status=session.status, flow=session.flow, close_reason=session.close_reason,
        attempts_remaining=max(0, settings.irrelevant_limit - session.irrelevant_count),
        irrelevant_limit=settings.irrelevant_limit, sound_enabled=session.sound_enabled,
        idle_check_after_seconds=settings.idle_check_after_seconds,
        idle_close_after_seconds=settings.idle_close_after_seconds, message_count=len(session.messages))


def message_out(m: Message) -> MessageOut:
    return MessageOut(seq=m.seq, role=m.role, content=m.content, kind=m.kind,
                      created_at=m.created_at.isoformat(), sources=m.sources)


def sources_out(rag) -> list[SourceOut]:
    if rag is None:
        return []
    return [SourceOut(number=s.number, title=s.title, url=s.url, source_type=s.source_type, platform=s.platform,
                      category=s.category, snippet=s.snippet) for s in rag.sources]


_SOUND_BY_KIND = {"session_end": "session_ended", "idle_check": "idle_check"}


def notification_for(session: Session, messages: list[Message]) -> Notification:
    """Which sound the frontend should play (None when sound is off or nothing new)."""
    assistant = [m for m in messages if m.role == "assistant"]
    if not session.sound_enabled or not assistant:
        return Notification()
    return Notification(sound=_SOUND_BY_KIND.get(assistant[-1].kind, "message_received"))
