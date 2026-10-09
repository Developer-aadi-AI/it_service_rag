"""Session and message models for multi-turn conversations."""
from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

Role = Literal["user", "assistant"]
SessionStatus = Literal["active", "awaiting_idle_response", "closed"]
Flow = Literal["awaiting_contact_form", "awaiting_anything_else"]
CloseReason = Literal["user_ended", "irrelevant_limit", "idle_timeout", "user_closed"]

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Message:
    seq: int
    role: Role
    content: str
    created_at: datetime
    # message | idle_check | session_end | lead_confirmation | contact_prompt | feedback_ack
    kind: str = "message"
    sources: list[dict[str, Any]] = field(default_factory=list)  # public: number, title, url
    meta: dict[str, Any] = field(default_factory=dict)  # internal: status, sentiment, ... (never emailed)


@dataclass
class Session:
    session_id: str
    created_at: datetime
    last_activity_at: datetime
    status: SessionStatus = "active"
    flow: Flow | None = None
    messages: list[Message] = field(default_factory=list)

    # conversation state
    topic: str | None = None  # title of the main page behind the last relevant answer
    last_relevant_query: str | None = None  # standalone query of the last relevant turn
    irrelevant_count: int = 0
    frustration_streak: int = 0
    last_acknowledged_seq: int | None = None
    recommended: list[dict[str, Any]] = field(default_factory=list)  # primary recommendations shown

    # customer + preferences
    customer_name: str | None = None
    customer_email: str | None = None
    sound_enabled: bool = True
    lead_ids: list[str] = field(default_factory=list)
    feedback: str | None = None

    # lifecycle
    idle_check_sent_at: datetime | None = None
    closed_at: datetime | None = None
    close_reason: CloseReason | None = None
    emails_sent: list[str] = field(default_factory=list)

    lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    @staticmethod
    def new(now: datetime) -> "Session":
        return Session(session_id=uuid.uuid4().hex, created_at=now, last_activity_at=now)

    @property
    def closed(self) -> bool:
        return self.status == "closed"

    @property
    def next_seq(self) -> int:
        return (self.messages[-1].seq + 1) if self.messages else 1

    def add(self, role: Role, content: str, now: datetime, max_messages: int, **kw: Any) -> Message:
        msg = Message(seq=self.next_seq, role=role, content=content, created_at=now, **kw)
        self.messages.append(msg)
        if len(self.messages) > max_messages:  # bounded memory; keep the most recent
            del self.messages[: len(self.messages) - max_messages]
        self.last_activity_at = now
        return msg

    def user_messages(self) -> list[Message]:
        return [m for m in self.messages if m.role == "user"]

    def llm_history(self, turns: int, max_chars: int = 1200) -> list[dict[str, str]]:
        """Last `turns` user/assistant pairs as LLM messages (controlled history)."""
        if turns <= 0:
            return []
        convo = [m for m in self.messages if m.kind in ("message", "lead_confirmation", "contact_prompt")]
        recent = convo[-2 * turns:]
        # [n] markers refer to the sources of *that* turn; drop them so the LLM
        # cannot confuse them with the numbering of the current context.
        out = [{"role": m.role, "content": re.sub(r"\s*\[\d{1,2}\]", "", m.content)[:max_chars]} for m in recent]
        while out and out[0]["role"] != "user":  # LLM APIs expect a user turn first
            out.pop(0)
        return out
