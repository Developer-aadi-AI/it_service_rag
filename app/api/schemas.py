"""Request/response models for the HTTP API."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.leads.service import LeadInput


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, examples=["What is included in Annual Support?"])
    top_k: int | None = Field(None, ge=1, le=20, description="override configured top-k")
    similarity_threshold: float | None = Field(None, ge=0.0, le=1.0,
                                               description="override configured similarity threshold")

    @field_validator("question")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("question must not be blank")
        return v


class SourceOut(BaseModel):
    number: int = Field(..., description="matches the [n] markers in the answer")
    title: str
    url: str
    source_type: str
    platform: str = ""
    category: str = ""
    snippet: str = ""


class DebugInfo(BaseModel):
    top_score: float
    generator: str
    mode: str
    latency_ms: int
    source_scores: dict[int, float] = {}


class AskResponse(BaseModel):
    request_id: str
    answer: str
    status: Literal["answered", "needs_team", "off_topic", "smalltalk", "error"]
    sources: list[SourceOut] = []
    action: dict[str, Any] | None = Field(None, description="UI action, e.g. a contact form to show")
    off_topic: bool = False
    request_kind: str = "other"
    debug: DebugInfo | None = Field(None, description="only outside production")


# The contact form model lives with the lead service; re-exported for the API.
ContactRequest = LeadInput


class ContactResponse(BaseModel):
    lead_id: str
    message: str


class HealthResponse(BaseModel):
    status: str
    mode: str
    documents: int
    chunks: int
    similarity_threshold: float
    domain_threshold: float
    top_k: int


# ============================ Phase 2: conversations ============================

class SessionCreate(BaseModel):
    name: str | None = Field(None, max_length=100)
    email: EmailStr | None = Field(None, description="optional: enables the summary/follow-up emails")
    sound_enabled: bool = True


class MessageOut(BaseModel):
    seq: int
    role: Literal["user", "assistant"]
    content: str
    kind: str
    created_at: str = Field(..., description="ISO 8601, UTC")
    sources: list[dict[str, Any]] = []


class SessionState(BaseModel):
    session_id: str
    status: Literal["active", "awaiting_idle_response", "closed"]
    flow: str | None = None
    close_reason: str | None = None
    attempts_remaining: int
    irrelevant_limit: int
    sound_enabled: bool
    idle_check_after_seconds: int
    idle_close_after_seconds: int
    message_count: int


class Notification(BaseModel):
    """Frontend hint: play a sound for this event when sound is enabled."""
    sound: Literal["message_received", "session_ended", "idle_check"] | None = None


class ChatDebug(BaseModel):
    sentiment: str | None = None
    search_query: str | None = None
    top_score: float | None = None
    generator: str | None = None


class SessionCreated(BaseModel):
    session: SessionState
    message: MessageOut


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


class ChatResponse(BaseModel):
    session: SessionState
    message: MessageOut
    status: str
    sources: list[SourceOut] = []
    recommendations: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    notification: Notification = Notification()
    debug: ChatDebug | None = None


class MessagesResponse(BaseModel):
    session: SessionState
    messages: list[MessageOut]
    notification: Notification = Notification()


class PreferencesUpdate(BaseModel):
    sound_enabled: bool


class FeedbackRequest(BaseModel):
    rating: Literal["Great", "OK", "Poor"]
    comment: str | None = Field(None, max_length=1000)


class FeedbackResponse(BaseModel):
    rating: str
    message: MessageOut


class EmailRequest(BaseModel):
    type: Literal["transcript", "summary", "followup", "feedback"]
    email: EmailStr | None = Field(None, description="defaults to the address given in the chat")


class EmailResponse(BaseModel):
    queued: bool
    message: str


class SessionContactResponse(BaseModel):
    lead_id: str
    session: SessionState
    message: MessageOut
