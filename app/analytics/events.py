"""Analytics event recording (privacy-conscious).

Events carry only what the summaries need: statuses, counts, latencies, service
names, ratings and error types. Customer names, emails, phone numbers and lead
messages are never recorded. Question text is optional (ANALYTICS_STORE_QUESTIONS),
PII-scrubbed and truncated, and used only for "top questions".
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Callable

from app.logging_config import scrub_pii

logger = logging.getLogger(__name__)

EVENT_TYPES = {
    "session_started", "message", "recommendation", "session_closed", "lead_submitted",
    "feedback", "email", "error", "ask",
}


def normalize_question(text: str, limit: int = 120) -> str:
    """Lower-cased, PII-masked, punctuation-trimmed question for grouping."""
    text = scrub_pii(" ".join((text or "").split())).lower()
    text = re.sub(r"[?!.]+$", "", text).strip()
    return text[:limit]


class EventRecorder:
    def __init__(self, db, clock: Callable[[], datetime], enabled: bool = True, store_questions: bool = True) -> None:
        self.db = db
        self.clock = clock
        self.enabled = enabled
        self.store_questions = store_questions

    def record(self, event_type: str, session_id: str | None = None, **data: Any) -> None:
        """Best effort: analytics must never break a customer request."""
        if not self.enabled:
            return
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {event_type}")
        if "question" in data:
            data["question"] = normalize_question(data["question"]) if self.store_questions else None
        try:
            self.db.execute("INSERT INTO events (created_at, event_type, session_id, data) VALUES (?, ?, ?, ?)",
                            (self.clock().isoformat(), event_type, session_id,
                             json.dumps(data, ensure_ascii=False, default=str)))
        except Exception:
            logger.exception("could not record analytics event %s", event_type)

    def error(self, component: str, exc: BaseException | str, session_id: str | None = None) -> None:
        kind = exc if isinstance(exc, str) else type(exc).__name__
        self.record("error", session_id, component=component, error_type=kind)
