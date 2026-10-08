"""Lead capture: contact form inside a chat session, and the standalone form."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_services, message_out, session_state
from app.api.schemas import ContactRequest, ContactResponse, SessionContactResponse
from app.services import Services

router = APIRouter(tags=["leads"])


@router.post("/sessions/{session_id}/contact", response_model=SessionContactResponse, status_code=201)
def submit_session_contact(session_id: str, body: ContactRequest,
                           svc: Services = Depends(get_services)) -> SessionContactResponse:
    """Contact/support form submitted from a chat. Confirms, then asks if anything else is needed."""
    result = svc.manager.submit_contact(session_id, body)
    return SessionContactResponse(lead_id=result.lead.lead_id, session=session_state(result.session, svc.settings),
                                  message=message_out(result.reply))


@router.post("/contact", response_model=ContactResponse, status_code=201)
def submit_contact(body: ContactRequest, svc: Services = Depends(get_services)) -> ContactResponse:
    """Standalone contact form (no chat session)."""
    lead = svc.leads.create(body, source="contact_form")
    svc.emails.notify_team(lead, None)
    return ContactResponse(
        lead_id=lead.lead_id,
        message=(f"Thank you, {lead.first_name}! We've received your details and the relevant V Group team will "
                 "connect with you shortly. Is there anything else I can help you with?"))
