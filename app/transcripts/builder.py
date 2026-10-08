"""Conversation transcripts (.txt) and summaries.

Transcripts contain only customer-facing content: dates, UTC timestamps, the
user's and the assistant's messages and the public source links. Internal
metadata (scores, statuses, sentiment) is never included.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.conversation.models import Session
from app.llm.base import LLMClient, LLMError

logger = logging.getLogger(__name__)

_CLOSE_LABELS = {
    "user_ended": "Conversation completed",
    "user_closed": "Closed by the customer",
    "idle_timeout": "Ended due to inactivity",
    "irrelevant_limit": "Ended after repeated questions outside supported topics",
}


def friendly_date(dt) -> str:
    return f"{dt.strftime('%A')}, {dt.day} {dt.strftime('%B %Y')}"  # Thursday, 8 October 2026


def utc_time(dt) -> str:
    return dt.strftime("%H:%M:%S UTC")


def utc_datetime(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _strip_markers(text: str) -> str:
    return re.sub(r"\s*\[\d+\]", "", text).strip()


def build_transcript(session: Session) -> str:
    lines = [
        "V Group - Chat Transcript",
        "=" * 60,
        f"Date:       {friendly_date(session.created_at)}",
        f"Started:    {utc_datetime(session.created_at)}",
    ]
    if session.closed_at:
        lines.append(f"Ended:      {utc_datetime(session.closed_at)}")
        lines.append(f"Status:     {_CLOSE_LABELS.get(session.close_reason or '', 'Closed')}")
    lines.append(f"Reference:  {session.session_id[:12]}")
    if session.customer_name:
        lines.append(f"Customer:   {session.customer_name}")
    lines.append("=" * 60)
    lines.append("")

    current_day = session.created_at.date()
    for m in session.messages:
        if m.created_at.date() != current_day:  # long chats across midnight
            current_day = m.created_at.date()
            lines += ["", f"--- {friendly_date(m.created_at)} ---", ""]
        speaker = "You" if m.role == "user" else "V Group Assistant"
        lines.append(f"[{utc_time(m.created_at)}] {speaker}:")
        for para in m.content.split("\n"):
            lines.append(f"    {para}" if para.strip() else "")
        if m.sources:
            lines.append("    Sources:")
            for s in m.sources:
                lines.append(f"      [{s.get('number')}] {s.get('title')} - {s.get('url')}")
        lines.append("")
    lines += ["-" * 60, "All times are in Coordinated Universal Time (UTC).", ""]
    return "\n".join(lines)


def transcript_filename(session: Session) -> str:
    stamp = session.created_at.strftime("%Y%m%d-%H%M")
    return f"vgroup-chat-transcript-{stamp}-{session.session_id[:8]}.txt"


def save_transcript(session: Session, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / transcript_filename(session)
    path.write_text(build_transcript(session), encoding="utf-8")
    return path


# --- summaries ------------------------------------------------------------------

_SUMMARY_SYSTEM = (
    "Summarise this customer chat with V Group's website assistant for an email to the customer. "
    "Write 2-4 short bullet points covering what the customer asked about and what information was "
    "provided. Use only facts stated in the chat; do not add offers, prices or promises that are not in it. "
    "Do not mention the assistant's internal workings. Reply with JSON only: {\"points\": [string]}"
)
_SUMMARY_SCHEMA = {"type": "object", "properties": {"points": {"type": "array", "items": {"type": "string"}}},
                   "required": ["points"], "additionalProperties": False}


def summarize(session: Session, llm: LLMClient | None = None) -> list[str]:
    """Customer-facing summary points (LLM when available, rule-based otherwise)."""
    relevant = [m for m in session.messages if m.role == "user" and m.meta.get("relevant")]
    if llm is not None and relevant:
        convo = "\n".join(f"{'Customer' if m.role == 'user' else 'Assistant'}: {_strip_markers(m.content)[:600]}"
                          for m in session.messages if m.kind in ("message", "lead_confirmation"))
        try:
            from app.rag.generator import parse_json_object

            raw = llm.complete(_SUMMARY_SYSTEM, [{"role": "user", "content": convo[:8000]}], json_schema=_SUMMARY_SCHEMA)
            points = [str(p).strip() for p in parse_json_object(raw).get("points", []) if str(p).strip()]
            if points:
                return points[:4]
        except (LLMError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("LLM summary failed (%s); using rule-based summary", exc)

    points: list[str] = []
    if relevant:
        asked = [m.content if len(m.content) <= 110 else m.content[:107].rsplit(" ", 1)[0] + "..." for m in relevant[:5]]
        points.append("You asked about: " + "; ".join(asked))
    services = []
    for rec in session.recommended:
        if rec["name"] not in services:
            services.append(rec["name"])
    if services:
        points.append("Services we discussed: " + ", ".join(services[:4]))
    if session.lead_ids:
        points.append("You shared your contact details, and our team will connect with you shortly.")
    if not points:
        points.append("Thank you for visiting V Group's website chat.")
    return points
