"""Lead / contact-request capture: validation, storage and team notification."""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.storage.database import Database, mask_email

logger = logging.getLogger(__name__)

_PHONE = re.compile(r"^[+()\d][\d\s().-]{6,19}$")
_NAME = re.compile(r"^[^\d<>{}\[\]@#$%^*=|\\/]+$")  # letters, spaces, apostrophes, hyphens, dots...
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class LeadInput(BaseModel):
    """Contact form fields (validated before anything is stored)."""

    name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    phone: str | None = Field(None, max_length=20)
    company: str | None = Field(None, max_length=120)
    message: str = Field(..., min_length=5, max_length=2000)
    request_id: str | None = Field(None, max_length=32, description="the /ask request that led here")

    @field_validator("name", "company", "message", mode="before")
    @classmethod
    def _clean_text(cls, v):
        if v is None:
            return v
        if not isinstance(v, str):
            raise ValueError("must be text")
        v = _CONTROL.sub("", v)
        return " ".join(v.split()) or None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _NAME.match(v):
            raise ValueError("name contains invalid characters")
        return v

    @field_validator("phone", mode="before")
    @classmethod
    def _phone(cls, v):
        if v in (None, ""):
            return None
        v = str(v).strip()
        if not _PHONE.match(v) or sum(c.isdigit() for c in v) < 7:
            raise ValueError("phone number looks invalid")
        return v


@dataclass
class Lead:
    lead_id: str
    created_at: str
    session_id: str | None
    name: str
    email: str
    phone: str | None
    company: str | None
    message: str
    interest: str | None
    source: str
    request_id: str | None = None

    @property
    def first_name(self) -> str:
        return self.name.split()[0]


class LeadCaptureError(RuntimeError):
    """The lead could not be stored (the customer is asked to retry)."""


def lead_confirmation(lead: "Lead") -> str:
    return (f"Thank you, {lead.first_name}! We've received your details, and the relevant D Group team will "
            "connect with you shortly. Is there anything else I can help you with?")


class LeadService:
    def __init__(self, db: Database, clock: Callable[[], datetime],
                 on_created: Callable[[Lead], None] | None = None) -> None:
        self.db = db
        self.clock = clock
        self.on_created = on_created  # e.g. notify the team by email

    def create(self, data: LeadInput, session_id: str | None = None, interest: str | None = None,
               source: str = "chat_form") -> Lead:
        lead = Lead(lead_id=uuid.uuid4().hex[:12], created_at=self.clock().isoformat(), session_id=session_id,
                    name=data.name, email=str(data.email), phone=data.phone, company=data.company,
                    message=data.message, interest=interest, source=source, request_id=data.request_id)
        self.db.execute(
            "INSERT INTO leads (lead_id, created_at, session_id, name, email, phone, company, message, "
            "interest, source, request_id) VALUES (:lead_id, :created_at, :session_id, :name, :email, :phone, "
            ":company, :message, :interest, :source, :request_id)", asdict(lead))
        logger.info("lead captured id=%s session=%s email=%s interest=%s", lead.lead_id, session_id,
                    mask_email(lead.email), interest)
        if self.on_created:
            try:
                self.on_created(lead)
            except Exception:  # notification failures must not lose the lead
                logger.exception("lead notification failed for %s", lead.lead_id)
        return lead

    def get(self, lead_id: str) -> Lead | None:
        rows = self.db.query("SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
        return Lead(**rows[0]) if rows else None

    def for_session(self, session_id: str) -> list[Lead]:
        return [Lead(**r) for r in self.db.query(
            "SELECT * FROM leads WHERE session_id = ? ORDER BY created_at", (session_id,))]
