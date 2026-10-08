"""Conversation orchestration on top of the Phase 1 RAG pipeline.

The manager owns conversation-level behaviour; grounded answering stays in
`RAGPipeline`. Per message:

1. record the user message (+ sentiment), clear a pending idle check;
2. handle conversation flows: "anything else?" yes/no, goodbye, explicit
   requests to talk to the team;
3. rewrite follow-ups into standalone search queries and call the pipeline
   with controlled history;
4. apply the irrelevant-input limit, recommendations and sentiment-aware
   wording; open the contact form when the team should follow up;
5. record the assistant message.

Idle handling (`check_idle`) and closing (`close`) also live here so the
API, the background monitor and the Gradio prototype share one implementation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from pydantic import EmailStr, TypeAdapter, ValidationError

from app.conversation import intents
from app.conversation.followup import FollowUpRewriter
from app.conversation.models import CloseReason, Message, Session, utc_now
from app.conversation.store import SessionClosed, SessionStore
from app.email.service import EmailService
from app.feedback.service import FEEDBACK_THANKS, RATINGS, FeedbackService
from app.leads.service import LeadInput, LeadService
from app.logging_config import redact
from app.rag import intent as rag_intent
from app.rag.pipeline import RAGPipeline, RAGResponse
from app.rag.prompts import CONTACT_FORM, INFORMATION_TEAM_REPLY, OFF_TOPIC_REPLY, SMALLTALK_REPLIES
from app.recommendations.engine import RecommendationEngine, is_recommendation_seeking, recommendation_sentence
from app.sentiment.analyzer import (CLARIFY_FRUSTRATED, ESCALATION_OFFER, RuleBasedSentimentAnalyzer, Sentiment, focus_query,
                                    SentimentAnalyzer, acknowledgement)
from app.transcripts.builder import save_transcript

logger = logging.getLogger(__name__)
_EMAIL = TypeAdapter(EmailStr)

CONTACT_PROMPT = ("Of course - I'd be happy to connect you with our team. Please share your details in the form "
                  "below, and the right V Group specialist will connect with you shortly.")
ANYTHING_ELSE_YES = "Of course - what else can I help you with?"
FEEDBACK_ACTION = {"type": "feedback", "prompt": "How was your experience today?", "options": list(RATINGS)}


def _attempts_text(remaining: int) -> str:
    noun = "attempt" if remaining == 1 else "attempts"
    return f"You have {remaining} more {noun} before this chat closes."


@dataclass
class TurnResult:
    session: Session
    reply: Message
    status: str  # answered | needs_team | off_topic | smalltalk | error | contact_prompt | session_end | ...
    action: dict[str, Any] | None = None
    recommendations: dict[str, Any] | None = None
    sentiment: Sentiment | None = None
    rag: RAGResponse | None = None
    lead: Any = None
    session_limit: int = 3

    @property
    def attempts_remaining(self) -> int:
        return max(0, self.session_limit - self.session.irrelevant_count)


class ConversationManager:
    def __init__(self, settings, pipeline: RAGPipeline, store: SessionStore, leads: LeadService,
                 emails: EmailService, feedback: FeedbackService,
                 recommender: RecommendationEngine | None = None,
                 rewriter: FollowUpRewriter | None = None,
                 analyzer: SentimentAnalyzer | None = None,
                 clock: Callable[[], datetime] = utc_now) -> None:
        self.settings = settings
        self.pipeline = pipeline
        self.store = store
        self.leads = leads
        self.emails = emails
        self.feedback = feedback
        self.recommender = recommender
        self.rewriter = rewriter
        self.analyzer = analyzer or RuleBasedSentimentAnalyzer()
        self.clock = clock

    # ------------------------------------------------------------------ helpers
    def _add(self, session: Session, role: str, content: str, **kw) -> Message:
        return session.add(role, content, self.clock(), self.settings.session_max_stored_messages, **kw)

    def _result(self, session: Session, reply: Message, status: str, **kw) -> TurnResult:
        return TurnResult(session=session, reply=reply, status=status,
                          session_limit=self.settings.irrelevant_limit, **kw)

    @staticmethod
    def _contact_action(reason: str) -> dict[str, Any]:
        return dict(CONTACT_FORM, reason=reason)

    @staticmethod
    def _asked_anything_else(session: Session) -> bool:
        for m in reversed(session.messages[:-1]):  # skip the current user message
            if m.role == "assistant":
                return "anything else" in m.content.lower()
        return False

    def _validate_text(self, text: str, limit: int) -> str:
        text = " ".join((text or "").split())
        if not text:
            raise ValueError("message must not be empty")
        if len(text) > limit:
            raise ValueError(f"message exceeds {limit} characters")
        return text

    def _open_session(self, session_id: str) -> Session:
        session = self.store.get(session_id)
        if session.closed:
            raise SessionClosed(session_id)
        return session

    # ------------------------------------------------------------------ lifecycle
    def start_session(self, name: str | None = None, email: str | None = None,
                      sound_enabled: bool = True) -> TurnResult:
        if email:
            email = str(_EMAIL.validate_python(email))
        session = self.store.create(self.clock())
        with session.lock:
            session.customer_name = " ".join(name.split())[:100] if name else None
            session.customer_email = email
            session.sound_enabled = sound_enabled
            greeting = self._add(session, "assistant", SMALLTALK_REPLIES["greeting"], kind="greeting")
        logger.info("session started id=%s", session.session_id[:8])
        return self._result(session, greeting, "greeting")

    def get(self, session_id: str) -> Session:
        return self.store.get(session_id)

    def set_preferences(self, session_id: str, sound_enabled: bool | None = None) -> Session:
        session = self.store.get(session_id)
        with session.lock:
            if sound_enabled is not None:
                session.sound_enabled = sound_enabled
        return session

    def messages_after(self, session_id: str, after_seq: int = 0) -> tuple[Session, list[Message]]:
        session = self.store.get(session_id)
        self.check_idle(session)  # lazy idle handling for polling clients
        with session.lock:
            return session, [m for m in session.messages if m.seq > after_seq]

    # ------------------------------------------------------------------ messages
    def handle_message(self, session_id: str, text: str) -> TurnResult:
        session = self._open_session(session_id)
        text = self._validate_text(text, self.settings.max_message_chars)
        with session.lock:
            if session.closed:
                raise SessionClosed(session_id)
            history = session.llm_history(self.settings.session_history_turns)
            sentiment = self.analyzer.analyze(text)
            user_msg = self._add(session, "user", text, meta={"sentiment": sentiment.label})
            if session.status == "awaiting_idle_response":  # the user is back
                session.status, session.idle_check_sent_at = "active", None
            result = self._respond(session, text, sentiment, user_msg, history)
            result.sentiment = sentiment
        logger.info("turn session=%s status=%s sentiment=%s irrelevant=%d q=%r", session.session_id[:8],
                    result.status, sentiment.label, session.irrelevant_count, redact(text))
        return result

    def _respond(self, session: Session, text: str, sentiment: Sentiment, user_msg: Message,
                 history: list[dict[str, str]]) -> TurnResult:
        # -- conversation flows ---------------------------------------------------
        if session.flow == "awaiting_anything_else":
            session.flow = None
            if intents.is_negative_reply(text):
                return self.close(session, "user_ended")
            if intents.is_affirmative_only(text):
                reply = self._add(session, "assistant", ANYTHING_ELSE_YES)
                return self._result(session, reply, "continue")
        if rag_intent.smalltalk_kind(text) == "goodbye" or (
                intents.is_negative_reply(text) and self._asked_anything_else(session)):
            return self.close(session, "user_ended")
        if intents.is_contact_request(text):
            ack = acknowledgement(sentiment.label, text) if sentiment.is_negative else ""
            session.flow = "awaiting_contact_form"
            reply = self._add(session, "assistant", f"{ack} {CONTACT_PROMPT}".strip(), kind="contact_prompt")
            user_msg.meta["relevant"] = True
            return self._result(session, reply, "contact_prompt", action=self._contact_action("contact_request"))

        # -- purely emotional messages: acknowledge instead of searching -----------------
        focus = focus_query(text, self.analyzer, rag_intent.mentions_business) if sentiment.is_negative else text
        if sentiment.is_negative and not focus:
            ack = acknowledgement(sentiment.label, text)
            if session.topic:  # unhappy about what we were discussing: offer the team, no strike
                user_msg.meta["relevant"] = True
                session.flow = "awaiting_contact_form"
                reply = self._add(session, "assistant", f"{ack} {ESCALATION_OFFER}",
                                  meta={"status": "needs_team"})
                return self._result(session, reply, "needs_team", action=self._contact_action("frustration"))
            session.irrelevant_count += 1  # unclear input: counts, but we ask what they need
            remaining = self.settings.irrelevant_limit - session.irrelevant_count
            if remaining <= 0:
                return self.close(session, "irrelevant_limit")
            reply = self._add(session, "assistant", f"{ack} {CLARIFY_FRUSTRATED}\n\n{_attempts_text(remaining)}",
                              meta={"status": "off_topic"})
            return self._result(session, reply, "off_topic")

        # -- grounded answer ---------------------------------------------------------
        query_text = focus if focus != text else text
        search_query = self.rewriter.rewrite(query_text, session) if self.rewriter else None
        if search_query is None and query_text != text:
            search_query = query_text  # retrieval ignores the emotional clauses
        rag = self.pipeline.answer(text, history, search_query=search_query)
        status, answer, action, recs = rag.status, rag.answer, rag.action, None
        relevant = status in ("answered", "needs_team")
        user_msg.meta.update(relevant=relevant, search_query=rag.search_query)

        if status == "off_topic":
            if sentiment.is_negative and session.topic:
                # frustrated about the topic we were discussing: hand off instead of a strike
                status, relevant = "needs_team", True
                user_msg.meta["relevant"] = True
                answer = f"{acknowledgement(sentiment.label, text)} {ESCALATION_OFFER}"
                action = self._contact_action("frustration")
            else:
                session.irrelevant_count += 1
                remaining = self.settings.irrelevant_limit - session.irrelevant_count
                if remaining <= 0:
                    return self.close(session, "irrelevant_limit")
                if sentiment.is_negative:  # unclear + upset: ask what they need
                    answer = f"{acknowledgement(sentiment.label, text)} {CLARIFY_FRUSTRATED}"
                else:
                    answer = OFF_TOPIC_REPLY
                answer = f"{answer}\n\n{_attempts_text(remaining)}"
        elif relevant:
            if status == "needs_team" and sentiment.is_negative and answer == INFORMATION_TEAM_REPLY:
                answer = ESCALATION_OFFER  # a complaint is not "a great question"
            if self.settings.irrelevant_reset_on_relevant:
                session.irrelevant_count = 0
            if rag.sources:
                session.topic = rag.sources[0].title
            session.last_relevant_query = rag.search_query

        # -- recommendations ---------------------------------------------------------
        if relevant and self.recommender and self.settings.recommendations_enabled:
            rec = self.recommender.recommend(rag.search_query, [s.page_id for s in rag.sources])
            if rec:
                recs = rec.public()
                session.recommended.append(recs["primary"])
                if status == "answered" and (is_recommendation_seeking(text) or rag.request_kind == "project_request"):
                    answer = _insert_before_closing(answer, recommendation_sentence(rec))

        # -- sentiment-aware wording ---------------------------------------------------
        if sentiment.label == "frustrated":
            session.frustration_streak += 1
        else:
            session.frustration_streak = 0
        if sentiment.is_negative and status in ("answered", "needs_team", "error") and not answer.startswith(
                tuple(a for a in ("I'm sorry", "Sorry", "I understand"))):
            recently = session.last_acknowledged_seq is not None and user_msg.seq - session.last_acknowledged_seq <= 2
            if not recently:
                answer = f"{acknowledgement(sentiment.label, text)} {answer}"
                session.last_acknowledged_seq = user_msg.seq
        if (session.frustration_streak >= self.settings.sentiment_escalate_after and action is None
                and status != "off_topic"):
            answer = f"{answer}\n\n{ESCALATION_OFFER}"
            action = self._contact_action("frustration")
        if action and action.get("type") == "contact_form":
            session.flow = "awaiting_contact_form"

        sources = [{"number": s.number, "title": s.title, "url": s.url} for s in rag.sources]
        reply = self._add(session, "assistant", answer, sources=sources,
                          meta={"status": status, "request_kind": rag.request_kind, "generator": rag.generator,
                                "top_score": rag.top_score})
        return self._result(session, reply, status, action=action, recommendations=recs, rag=rag)

    # ------------------------------------------------------------------ contact / leads
    def submit_contact(self, session_id: str, data: LeadInput) -> TurnResult:
        session = self._open_session(session_id)
        with session.lock:
            interest = session.recommended[-1]["name"] if session.recommended else session.topic
            lead = self.leads.create(data, session_id=session.session_id, interest=interest)
            session.lead_ids.append(lead.lead_id)
            session.customer_name = session.customer_name or lead.name
            session.customer_email = session.customer_email or lead.email
            session.flow = "awaiting_anything_else"
            self._add(session, "user", "(Submitted the contact form)", kind="form_submission")
            reply = self._add(session, "assistant",
                              f"Thank you, {lead.first_name}! We've received your details, and the relevant "
                              f"V Group team will connect with you shortly. Is there anything else I can help you with?",
                              kind="lead_confirmation")
        self.emails.notify_team(lead, session)
        return self._result(session, reply, "lead_confirmation", lead=lead)

    # ------------------------------------------------------------------ feedback / email
    def record_feedback(self, session_id: str, rating: str, comment: str | None = None,
                        channel: str = "chat") -> Message:
        session = self.store.get(session_id)
        r = self.feedback.record(session.session_id, rating, comment, channel)
        with session.lock:
            session.feedback = r
            return self._add(session, "assistant", FEEDBACK_THANKS[r], kind="feedback_ack")

    def send_email(self, session_id: str, kind: str, to: str | None = None):
        session = self.store.get(session_id)
        address = to or session.customer_email
        if not address:
            raise ValueError("an email address is required")
        address = str(_EMAIL.validate_python(address))
        if not self.emails.enabled:
            raise RuntimeError("email is not configured")
        with session.lock:
            session.customer_email = session.customer_email or address
        return self.emails.send(kind, session, address)

    # ------------------------------------------------------------------ closing / idle
    def _will_email_on_close(self, session: Session, reason: CloseReason) -> bool:
        if not (session.customer_email and self.emails.enabled):
            return False
        return self.settings.email_on_idle_close if reason == "idle_timeout" else self.settings.email_on_close

    def close(self, session: Session, reason: CloseReason) -> TurnResult:
        with session.lock:
            if session.closed:
                return self._result(session, session.messages[-1], "session_end")
            emailing = self._will_email_on_close(session, reason)
            note = " A summary of our conversation is on its way to your inbox." if emailing else ""
            action = None
            if reason == "idle_timeout":
                text = (f"This chat has ended because we didn't hear back from you.{note} Feel free to start a "
                        f"new chat anytime - we're happy to help.")
            elif reason == "irrelevant_limit":
                text = ("Sorry, that's also outside the V Group information I can help with. Since the last few "
                        "messages weren't about V Group's services, I'm closing this chat for now."
                        f"{note} If you have questions about our services, products or support plans, you're "
                        "welcome to start a new chat anytime. Thank you!")
                action = FEEDBACK_ACTION
            else:
                text = f"Thank you for chatting with V Group!{note} Have a great day."
                action = FEEDBACK_ACTION
            reply = self._add(session, "assistant", text, kind="session_end")
            session.status, session.flow = "closed", None
            session.closed_at, session.close_reason = self.clock(), reason
            if self.settings.save_transcripts_on_close:
                try:
                    save_transcript(session, self.settings.transcripts_path)
                except OSError:
                    logger.exception("could not save transcript for %s", session.session_id[:8])
        if emailing:
            self.emails.on_session_closed(session)
        logger.info("session closed id=%s reason=%s messages=%d", session.session_id[:8], reason,
                    len(session.messages))
        return self._result(session, reply, "session_end", action=action)

    def close_by_user(self, session_id: str) -> TurnResult:
        return self.close(self.store.get(session_id), "user_closed")

    def check_idle(self, session: Session, now: datetime | None = None) -> list[Message]:
        """Send the 'are you still there?' check, then close if there is still no reply."""
        now = now or self.clock()
        with session.lock:
            if session.closed:
                return []
            if session.status == "active":
                idle = (now - session.last_activity_at).total_seconds()
                if idle >= self.settings.idle_check_after_seconds and session.user_messages():
                    wait = self.settings.idle_close_after_seconds
                    when = f"{wait // 60} minute{'s' if wait >= 120 else ''}" if wait >= 60 else f"{wait} seconds"
                    msg = self._add(session, "assistant",
                                    f"Are you still there? Just reply to keep chatting - otherwise this chat will "
                                    f"close automatically in about {when}.", kind="idle_check")
                    session.status, session.idle_check_sent_at = "awaiting_idle_response", now
                    return [msg]
                return []
            if session.status == "awaiting_idle_response" and session.idle_check_sent_at:
                waited = (now - session.idle_check_sent_at).total_seconds()
                if waited >= self.settings.idle_close_after_seconds:
                    return [self.close(session, "idle_timeout").reply]
        return []

    def sweep(self) -> int:
        """Run idle checks for all sessions and purge expired ones. Returns messages created."""
        now = self.clock()
        created = 0
        for session in self.store.all():
            try:
                created += len(self.check_idle(session, now))
            except Exception:
                logger.exception("idle check failed for %s", session.session_id[:8])
        self.store.purge(now, self.settings.session_retention_seconds)
        return created


def _insert_before_closing(answer: str, sentence: str) -> str:
    """Put a recommendation before the friendly closing question, if there is one."""
    head, sep, tail = answer.rpartition("\n\n")
    if sep and tail.strip().endswith("?") and len(tail) < 120:
        return f"{head}\n\n{sentence}\n\n{tail}"
    return f"{answer}\n\n{sentence}"
