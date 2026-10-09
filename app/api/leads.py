"""Lead capture: contact form inside a chat session, and the standalone form."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.api.deps import get_services, message_out, session_state
from app.api.schemas import ContactRequest, ContactResponse, ErrorResponse, SessionContactResponse
from app.leads.service import LeadCaptureError, lead_confirmation
from app.services import Services

logger = logging.getLogger(__name__)
router = APIRouter(tags=["leads"])
_ERRORS = {404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse},
           503: {"model": ErrorResponse}}


@router.post("/sessions/{session_id}/contact", response_model=SessionContactResponse, status_code=201,
             responses=_ERRORS, summary="Submit the contact form from a chat")
def submit_session_contact(session_id: str, body: ContactRequest,
                           svc: Services = Depends(get_services)) -> SessionContactResponse:
    """Contact/support form submitted from a chat. Confirms, then asks if anything else is needed."""
    result = svc.manager.submit_contact(session_id, body)
    return SessionContactResponse(lead_id=result.lead.lead_id, session=session_state(result.session, svc.settings),
                                  message=message_out(result.reply))


@router.post("/contact", response_model=ContactResponse, status_code=201,
             responses={422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
             summary="Standalone contact form")
def submit_contact(body: ContactRequest, svc: Services = Depends(get_services)) -> ContactResponse:
    """Standalone contact form (no chat session)."""
    try:
        lead = svc.leads.create(body, source="contact_form")
    except Exception as exc:
        logger.exception("lead capture failed (standalone form)")
        svc.events.error("leads", exc)
        raise LeadCaptureError() from exc
    svc.events.record("lead_submitted", None, interest=None, source=lead.source,
                      with_phone=bool(lead.phone), with_company=bool(lead.company))
    svc.emails.notify_team(lead, None)
    return ContactResponse(lead_id=lead.lead_id, message=lead_confirmation(lead))
