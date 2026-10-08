"""Feedback: from the chat widget, or one-click links in emails."""
from __future__ import annotations

from html import escape

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse

from app.api.deps import get_services, message_out
from app.api.schemas import FeedbackRequest, FeedbackResponse
from app.conversation.store import SessionNotFound
from app.feedback.service import FEEDBACK_THANKS, InvalidFeedback
from app.services import Services

router = APIRouter(tags=["feedback"])


@router.post("/sessions/{session_id}/feedback", response_model=FeedbackResponse)
def chat_feedback(session_id: str, body: FeedbackRequest, svc: Services = Depends(get_services)) -> FeedbackResponse:
    """Great / OK / Poor (also accepted after the chat has ended)."""
    msg = svc.manager.record_feedback(session_id, body.rating, body.comment, channel="chat")
    return FeedbackResponse(rating=body.rating, message=message_out(msg))


def _page(title: str, text: str, status: int = 200) -> HTMLResponse:
    html = ("<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
            "content=\"width=device-width,initial-scale=1\"><title>V Group feedback</title></head>"
            "<body style=\"font-family:Arial,Helvetica,sans-serif;background:#f5f6f8;margin:0;padding:40px 16px\">"
            "<div style=\"max-width:480px;margin:0 auto;background:#fff;border-radius:8px;padding:28px\">"
            f"<h2 style=\"margin-top:0\">{escape(title)}</h2><p>{escape(text)}</p></div></body></html>")
    return HTMLResponse(html, status_code=status)


@router.get("/feedback/{token}", response_class=HTMLResponse, include_in_schema=True)
def email_feedback(token: str, rating: str = Query(..., max_length=10), svc: Services = Depends(get_services)):
    """Target of the Great / OK / Poor buttons in emails (signed link)."""
    try:
        session_id = svc.feedback.session_from_token(token)
        r = svc.feedback.record(session_id, rating, channel="email")
    except InvalidFeedback:
        return _page("Link not valid", "This feedback link is not valid. Thank you for wanting to share feedback!", 400)
    try:
        session = svc.manager.get(session_id)
        with session.lock:
            session.feedback = r
    except SessionNotFound:
        pass  # session may have expired from memory; feedback is still stored
    return _page("Thank you!", FEEDBACK_THANKS[r])
