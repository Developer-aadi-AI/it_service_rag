"""Transcript download and email requests."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from app.api.deps import get_services
from app.api.schemas import EmailRequest, EmailResponse
from app.services import Services
from app.transcripts.builder import build_transcript, transcript_filename

router = APIRouter(prefix="/sessions/{session_id}", tags=["transcripts & email"])

_EMAIL_CONFIRMATION = {
    "transcript": "The chat transcript is on its way to your inbox.",
    "summary": "A summary of our conversation is on its way to your inbox.",
    "followup": "A follow-up email is on its way to your inbox.",
    "feedback": "A short feedback request is on its way to your inbox.",
}


@router.get("/transcript", response_class=PlainTextResponse)
def download_transcript(session_id: str, svc: Services = Depends(get_services)) -> PlainTextResponse:
    """Plain-text (.txt) transcript with a dated header and UTC timestamps."""
    session = svc.manager.get(session_id)
    with session.lock:
        text, filename = build_transcript(session), transcript_filename(session)
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/emails", response_model=EmailResponse, status_code=202)
def request_email(session_id: str, body: EmailRequest, svc: Services = Depends(get_services)) -> EmailResponse:
    """Email the transcript, a summary, a follow-up or a feedback request."""
    try:
        future = svc.manager.send_email(session_id, body.type, str(body.email) if body.email else None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Email is not available right now.") from exc
    return EmailResponse(queued=future is not None, message=_EMAIL_CONFIRMATION[body.type])
