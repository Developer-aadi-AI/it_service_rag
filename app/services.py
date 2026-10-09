"""Wire Phase 2 services around the Phase 1 pipeline (one place, easy to override in tests)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from app.analytics.events import EventRecorder
from app.config import Settings
from app.conversation.followup import FollowUpRewriter
from app.conversation.manager import ConversationManager
from app.conversation.models import utc_now
from app.conversation.store import InMemorySessionStore, SessionStore
from app.email.senders import EmailSender, create_sender
from app.email.service import EmailService
from app.feedback.service import FeedbackService
from app.leads.service import LeadService
from app.rag.pipeline import PipelineBundle
from app.recommendations.engine import RecommendationEngine, ServiceCatalog
from app.storage.database import Database


@dataclass
class Services:
    settings: Settings
    bundle: PipelineBundle
    db: Database
    store: SessionStore
    leads: LeadService
    feedback: FeedbackService
    emails: EmailService
    catalog: ServiceCatalog
    manager: ConversationManager
    events: EventRecorder

    def shutdown(self) -> None:
        self.emails.shutdown()
        self.db.close()


def build_services(settings: Settings, bundle: PipelineBundle, *, clock: Callable[[], datetime] = utc_now,
                   email_sender: EmailSender | None = None, background_email: bool = True,
                   store: SessionStore | None = None) -> Services:
    pipeline = bundle.pipeline
    db = Database(settings.db_path)
    events = EventRecorder(db, clock, settings.analytics_enabled, settings.analytics_store_questions)
    pipeline.events = events
    feedback = FeedbackService(db, clock, settings.app_secret_key)
    emails = EmailService(email_sender or create_sender(settings), settings, db, feedback,
                          contact=pipeline.contact, llm=pipeline.llm, clock=clock, background=background_email,
                          events=events)
    leads = LeadService(db, clock)
    catalog = ServiceCatalog.from_store(bundle.store)
    recommender = RecommendationEngine(catalog, pipeline.retriever.embedder,
                                       settings.recommendation_min_similarity, settings.recommendation_related_count)
    rewriter = FollowUpRewriter(pipeline.retriever, pipeline.llm, settings.domain_threshold)
    store = store or InMemorySessionStore()
    manager = ConversationManager(settings, pipeline, store, leads, emails, feedback, recommender=recommender,
                                  rewriter=rewriter, clock=clock, events=events)
    return Services(settings, bundle, db, store, leads, feedback, emails, catalog, manager, events)
