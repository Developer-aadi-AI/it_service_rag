"""Core RAG pipeline: question -> retrieval -> decision -> grounded answer + sources.

Decision flow
-------------
1. Small talk (hi / thanks / bye)            -> friendly fixed reply (not off-topic).
2. Retrieve (hybrid dense + BM25).
3. top_score >= similarity_threshold          -> generator answers from context
                                                 (or says needs_team / off_topic).
4. domain_threshold <= top_score < threshold  -> no specific answer, but in D Group's
                                                 domain -> triage -> team hand-off + contact form.
5. below domain_threshold                     -> off-topic (an LLM triage may still
                                                 rescue clearly business-related questions).

The user is never told "no answer found": unanswered in-domain questions get a
team hand-off ("That sounds like a great idea!" for project ideas).
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.embeddings.base import Embedder, create_embedder
from app.ingestion.pipeline import IndexReport, ensure_index
from app.llm.base import LLMClient, LLMError, create_llm
from app.logging_config import redact
from app.rag import intent
from app.rag.intent import is_company_fact as rag_company_fact
from app.rag.contact_info import ContactInfo, compose_contact_answer, is_contact_question, parse_contact_page
from app.rag.generator import ExtractiveGenerator, GenerationResult, LLMGenerator, Triage
from app.rag.prompts import (CONTACT_FORM, ERROR_REPLY, INFORMATION_TEAM_REPLY, OFF_TOPIC_REPLY,
                             PROJECT_IDEA_REPLY, SMALLTALK_REPLIES, ContextSource, group_sources)
from app.retrieval.retriever import Retriever
from app.vectorstore.base import VectorStore, create_vector_store

logger = logging.getLogger(__name__)


@dataclass
class Source:
    number: int
    title: str
    url: str
    source_type: str
    platform: str
    category: str
    page_id: str
    score: float
    snippet: str


@dataclass
class RAGResponse:
    request_id: str
    question: str
    answer: str
    status: str  # answered | needs_team | off_topic | smalltalk | error
    sources: list[Source] = field(default_factory=list)
    action: dict[str, Any] | None = None
    off_topic: bool = False
    request_kind: str = "other"
    generator: str = ""
    top_score: float = 0.0
    latency_ms: int = 0
    search_query: str = ""  # query actually used for retrieval


def _is_company_page(meta: dict) -> bool:
    """About/Home pages and the Services overview page."""
    return meta.get("source_type") in ("about", "home") or meta.get("url", "").rstrip("/").endswith("/services")


class RAGPipeline:
    def __init__(self, settings: Settings, retriever: Retriever, llm: LLMClient | None,
                 embedder: Embedder, capabilities: str = "") -> None:
        self.events = None  # optional analytics EventRecorder (set by build_services)
        self.settings = settings
        self.retriever = retriever
        self.llm = llm
        self.extractive = ExtractiveGenerator(embedder, min_score=settings.extractive_min_score)
        self.generator = LLMGenerator(llm, settings.max_context_chars) if llm else self.extractive
        self.triage = Triage(llm, capabilities)
        try:
            self.contact = parse_contact_page(retriever.store.all_chunks())
        except Exception:  # contact shortcut is optional
            logger.warning("could not parse contact page", exc_info=True)
            self.contact = ContactInfo()

    @property
    def mode(self) -> str:
        return f"llm:{self.llm.provider}/{self.llm.model}" if self.llm else "extractive"

    # -- helpers ------------------------------------------------------------------
    @staticmethod
    def _sources(sources: list[ContextSource], numbers: list[int]) -> list[Source]:
        by_num = {s.number: s for s in sources}
        out = []
        for n in numbers:
            s = by_num.get(n)
            if not s:
                continue
            best = max(s.hits, key=lambda h: h.score)
            snippet = best.text if len(best.text) <= 240 else best.text[:240].rsplit(" ", 1)[0] + "..."
            out.append(Source(s.number, s.title, s.url, s.source_type, s.platform, s.category,
                              s.page_id, round(s.best_score, 4), snippet))
        return out

    def _team_handoff(self, resp: RAGResponse, kind: str) -> RAGResponse:
        resp.status = "needs_team"
        resp.request_kind = kind
        resp.answer = PROJECT_IDEA_REPLY if kind == "project_request" else INFORMATION_TEAM_REPLY
        resp.action = dict(CONTACT_FORM, reason=kind)
        return resp

    @staticmethod
    def _off_topic(resp: RAGResponse) -> RAGResponse:
        resp.status, resp.off_topic, resp.answer = "off_topic", True, OFF_TOPIC_REPLY
        return resp

    def _generate(self, question: str, sources: list[ContextSource],
                  history: list[dict[str, str]] | None, overview: bool = False) -> GenerationResult:
        try:
            return self.generator.generate(question, sources, history, overview=overview)
        except LLMError as exc:
            if self.generator is self.extractive:
                raise
            logger.error("LLM generation failed (%s); falling back to extractive answer", exc)
            if self.events is not None:
                self.events.error("llm", exc)
            query = intent.OVERVIEW_QUERY if overview else question
            return self.extractive.generate(query, sources, overview=overview)

    # -- main entry point -----------------------------------------------------------
    def answer(self, question: str, history: list[dict[str, str]] | None = None,
               top_k: int | None = None, similarity_threshold: float | None = None,
               search_query: str | None = None) -> RAGResponse:
        """Answer one message.

        `history` (prior user/assistant turns) is passed to the LLM. `search_query`
        optionally replaces the question for retrieval and offline answer selection,
        e.g. a follow-up rewritten as a standalone question by the conversation layer.
        """
        start = time.perf_counter()
        question = " ".join((question or "").split())
        if not question:
            raise ValueError("question must not be empty")
        if len(question) > self.settings.max_question_chars:
            raise ValueError(f"question exceeds {self.settings.max_question_chars} characters")
        if history:
            history = history[-2 * self.settings.max_history_turns:] if self.settings.max_history_turns else []

        resp = RAGResponse(request_id=uuid.uuid4().hex[:12], question=question, answer="", status="")
        search_query = " ".join(search_query.split()) if search_query else None
        resp.search_query = search_query or question
        try:
            self._answer(resp, question, history, top_k, similarity_threshold, search_query)
        except Exception:  # never leak internals to the user
            logger.exception("pipeline failure request_id=%s", resp.request_id)
            if self.events is not None:
                self.events.error("rag", "PipelineError")
            resp.status, resp.answer, resp.sources = "error", ERROR_REPLY, []
            resp.action = dict(CONTACT_FORM, reason="error")
        resp.latency_ms = int((time.perf_counter() - start) * 1000)
        logger.info("ask id=%s status=%s kind=%s top=%.3f sources=%d gen=%s %dms q=%r",
                    resp.request_id, resp.status, resp.request_kind, resp.top_score, len(resp.sources),
                    resp.generator, resp.latency_ms, redact(question))
        return resp

    def _answer(self, resp: RAGResponse, question: str, history, top_k, threshold,
                rewritten: str | None = None) -> None:
        kind = intent.smalltalk_kind(question)
        if kind:
            resp.status, resp.answer, resp.generator = "smalltalk", SMALLTALK_REPLIES[kind], "rules"
            return

        # Unclear input with no words ("???", "...", "12345"): nothing to search for.
        if not re.search(r"[^\W\d_]{2,}", question):
            resp.generator = "rules"
            self._off_topic(resp)
            return

        # Contact details: the Contact Us page is a label block, so in offline mode its
        # parsed fields are stated directly (an LLM reads the page from context instead).
        if (self.llm is None and self.contact.available and is_contact_question(question)
                and not intent.is_project_request(question)):
            answer = compose_contact_answer(question, self.contact)
            if answer:
                c = self.contact
                snippet = c.hit.text[:240].rsplit(" ", 1)[0] + "..." if c.hit else ""
                resp.status, resp.answer, resp.generator = "answered", answer, "contact-info"
                resp.request_kind, resp.top_score = "information_request", 1.0
                resp.sources = [Source(1, c.title, c.url, "contact", "", "Contact & Partnership",
                                       c.page_id, 1.0, snippet)]
                return

        threshold = self.settings.similarity_threshold if threshold is None else threshold
        # Company-overview questions ("what do you guys do?") are mostly stop words, so
        # retrieval uses an explicit overview query restricted to the company pages;
        # they are never treated as off-topic.
        overview = intent.is_company_overview(question)
        search_query = intent.OVERVIEW_QUERY if overview else (rewritten or question)
        resp.search_query = search_query
        company_scope = overview or rag_company_fact(question)
        result = self.retriever.retrieve(
            search_query, top_k=top_k, similarity_threshold=threshold,
            metadata_filter=_is_company_page if company_scope else None)
        if company_scope and not result.has_context:  # fall back to unrestricted search
            result = self.retriever.retrieve(search_query, top_k=top_k, similarity_threshold=threshold)
        resp.top_score = round(result.top_score, 4)

        logger.debug("retrieval q=%r top=%.3f hits=%s", redact(search_query), result.top_score,
                     [(h.metadata.get("page_id"), round(h.score, 3)) for h in result.hits])
        if result.has_context:
            sources = group_sources(result.hits)
            # Offline answers are selected against the (possibly rewritten) search query;
            # the LLM reads the user's own words plus the conversation history.
            gen_question = search_query if self.generator is self.extractive else question
            gen = self._generate(gen_question, sources, history, overview)
            resp.generator = gen.generator
            if gen.status == "answered":
                resp.status, resp.answer, resp.request_kind = "answered", gen.answer, gen.request_kind
                resp.sources = self._sources(sources, gen.citations)
                return
            if gen.status == "off_topic":
                if self.llm is not None and intent.is_project_request(question):
                    # strong retrieval match + "can you build X": ask the feasibility triage instead
                    category = self.triage.classify(question)
                    if category != "off_topic":
                        resp.generator = "triage-llm"
                        self._team_handoff(resp, category)
                        return
                self._off_topic(resp)
                return
            kind = gen.request_kind if gen.request_kind != "other" else (
                "project_request" if intent.is_project_request(question) else "information_request")
            self._team_handoff(resp, kind)
            return

        if overview:
            self._team_handoff(resp, "information_request")
            return
        in_domain = result.top_score >= self.settings.domain_threshold
        if not in_domain and not (self.llm and intent.is_business_related(question)):
            resp.generator = "rules"
            self._off_topic(resp)
            return
        category = self.triage.classify(question)
        resp.generator = "triage-llm" if self.llm else "triage-rules"
        if category == "off_topic":
            self._off_topic(resp)
        else:
            self._team_handoff(resp, category)


def capabilities_summary(store: VectorStore, limit: int = 60) -> str:
    """List of offering names taken from service/product/pricing page titles."""
    titles: list[str] = []
    for chunk in store.all_chunks():
        m = chunk.metadata
        if m.get("category") in ("Services", "Hire Developers", "Shopify Apps", "Pricing") or \
                m.get("source_type") == "app-product":
            t = m.get("title", "").strip()
            if t and t not in titles:
                titles.append(t)
    return ", ".join(titles[:limit])


@dataclass
class PipelineBundle:
    pipeline: RAGPipeline
    index_report: IndexReport
    store: VectorStore


def build_pipeline(settings: Settings, embedder: Embedder | None = None,
                   store: VectorStore | None = None, llm: LLMClient | None | str = "auto") -> PipelineBundle:
    """Wire up all components. Pass objects to override (used in tests)."""
    embedder = embedder or create_embedder(settings)
    store = store or create_vector_store(settings)
    report = ensure_index(settings, embedder, store)
    retriever = Retriever(embedder, store, top_k=settings.top_k,
                          similarity_threshold=settings.similarity_threshold,
                          candidate_k=settings.candidate_k,
                          max_chunks_per_page=settings.max_chunks_per_page,
                          lexical_weight=settings.lexical_weight)
    llm_client = create_llm(settings) if llm == "auto" else llm
    pipeline = RAGPipeline(settings, retriever, llm_client, embedder, capabilities_summary(store))
    return PipelineBundle(pipeline, report, store)
