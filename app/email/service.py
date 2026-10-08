"""Email workflows: transcript, summary, follow-up, feedback request, team lead alert.

Emails are built from a snapshot of the session and sent in a background
thread so the chat never waits on SMTP. Every attempt is written to the
email_log table with the recipient masked.
"""
from __future__ import annotations

import copy
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Literal

from app.conversation.models import Session
from app.email import templates as tpl
from app.email.senders import Attachment, EmailSender, OutgoingEmail
from app.feedback.service import RATINGS, FeedbackService
from app.leads.service import Lead
from app.llm.base import LLMClient
from app.rag.contact_info import ContactInfo
from app.storage.database import Database, mask_email
from app.transcripts.builder import build_transcript, friendly_date, summarize, transcript_filename

logger = logging.getLogger(__name__)

EmailType = Literal["transcript", "summary", "followup", "feedback"]


class EmailService:
    def __init__(self, sender: EmailSender, settings, db: Database, feedback: FeedbackService,
                 contact: ContactInfo | None = None, llm: LLMClient | None = None,
                 clock: Callable | None = None, background: bool = True) -> None:
        self.sender = sender
        self.settings = settings
        self.db = db
        self.feedback = feedback
        self.llm = llm
        self.clock = clock
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="email") if background else None
        office = contact.offices[0] if contact and contact.offices else None
        self.footer = tpl.Footer(email=(contact.email if contact else ""),
                                 phone=(office.phone if office else ""),
                                 contact_url=(contact.url if contact else ""))
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.settings.email_backend != "disabled"

    # -- low level -------------------------------------------------------------------
    def _deliver(self, email: OutgoingEmail) -> bool:
        status, error = "sent", None
        try:
            self.sender.send(email)
        except Exception as exc:  # never crash the chat because of email
            status, error = "failed", type(exc).__name__
            logger.error("email %s to %s failed: %s", email.email_type, mask_email(email.to), exc)
        try:
            now = (self.clock() if self.clock else None)
            self.db.execute(
                "INSERT INTO email_log (created_at, session_id, email_type, recipient, status, error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ((now.isoformat() if now else ""), email.session_id, email.email_type, mask_email(email.to),
                 status, error))
        except Exception:
            logger.exception("could not write email log")
        if status == "sent":
            logger.info("email %s sent to %s via %s", email.email_type, mask_email(email.to), self.sender.name)
        return status == "sent"

    def _submit(self, build: Callable[[], OutgoingEmail | None]) -> Future | None:
        def job() -> bool:
            email = build()
            return self._deliver(email) if email else False

        if not self.enabled:
            return None
        if self._executor is None:
            f: Future = Future()
            f.set_result(job())
            return f
        return self._executor.submit(job)

    def _feedback_links(self, session_id: str) -> dict[str, str]:
        token = self.feedback.token_for(session_id)
        base = self.settings.public_base_url.rstrip("/")
        return {r: f"{base}/feedback/{token}?rating={r}" for r in RATINGS}

    def _attachment(self, snap: Session) -> Attachment:
        return Attachment(transcript_filename(snap), build_transcript(snap).encode("utf-8"), "text/plain")

    @staticmethod
    def _snapshot(session: Session) -> Session:
        with session.lock:
            snap = copy.copy(session)
            snap.messages = list(session.messages)
            snap.recommended = list(session.recommended)
            snap.lead_ids = list(session.lead_ids)
        return snap

    # -- workflows -------------------------------------------------------------------
    def send(self, kind: EmailType, session: Session, to: str) -> Future | None:
        """Send one email of the given type for the session."""
        snap = self._snapshot(session)
        date_text = friendly_date(snap.created_at)
        reply_to = self.settings.email_reply_to

        def build() -> OutgoingEmail:
            if kind == "transcript":
                r = tpl.transcript_email(snap.customer_name, date_text, self.footer)
                atts = [self._attachment(snap)]
            elif kind == "summary":
                attach = self.settings.email_attach_transcript
                r = tpl.summary_email(snap.customer_name, date_text, summarize(snap, self.llm), self.footer,
                                      attach, self._feedback_links(snap.session_id))
                atts = [self._attachment(snap)] if attach else []
            elif kind == "followup":
                attach = self.settings.email_attach_transcript
                reason = snap.close_reason if snap.close_reason == "idle_timeout" else "contact"
                r = tpl.followup_email(snap.customer_name, date_text, reason, summarize(snap, self.llm),
                                       self.footer, attach)
                atts = [self._attachment(snap)] if attach else []
            elif kind == "feedback":
                r = tpl.feedback_email(snap.customer_name, date_text, self._feedback_links(snap.session_id),
                                       self.footer)
                atts = []
            else:  # pragma: no cover - guarded by the API schema
                raise ValueError(kind)
            return OutgoingEmail(to=to, subject=r.subject, text=r.text, html=r.html, attachments=atts,
                                 email_type=kind, session_id=snap.session_id, reply_to=reply_to)

        with session.lock:
            session.emails_sent.append(kind)
        return self._submit(build)

    def on_session_closed(self, session: Session) -> Future | None:
        """Automatic email when a chat ends (where configured and an address is known)."""
        to = session.customer_email
        if not to or not self.enabled:
            return None
        if session.close_reason == "idle_timeout":
            return self.send("followup", session, to) if self.settings.email_on_idle_close else None
        if self.settings.email_on_close:
            return self.send("summary", session, to)  # summary + transcript + feedback links
        return None

    def notify_team(self, lead: Lead, session: Session | None) -> Future | None:
        to = self.settings.team_notification_email
        if not to:
            return None
        snap = self._snapshot(session) if session else None

        def build() -> OutgoingEmail:
            points = summarize(snap, None) if snap else []
            r = tpl.team_lead_email(lead, points)
            atts = [self._attachment(snap)] if snap else []
            return OutgoingEmail(to=to, subject=r.subject, text=r.text, html=r.html, attachments=atts,
                                 email_type="team_lead", session_id=lead.session_id, reply_to=lead.email)

        return self._submit(build)

    def shutdown(self) -> None:
        if self._executor:
            self._executor.shutdown(wait=True)
