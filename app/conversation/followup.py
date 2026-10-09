"""Turn follow-up messages into standalone search queries.

"How much does the Shopify Gold plan cost?" -> "And how long does it take?"
needs the previous topic to retrieve the right page. With an LLM the message
is rewritten by the model; otherwise two heuristics are used:

* anaphora ("it", "that", "they"...): prefix the previous topic;
* ellipsis ("what about BigCommerce?"): substitute the new subject into the
  previous question.

The rewrite is only kept when it retrieves at least as well as the original
message, so a standalone question that merely contains "it" is not hijacked.
"""
from __future__ import annotations

import json
import logging
import re

from app.conversation.models import Session
from app.llm.base import LLMClient, LLMError
from app.rag import intent
from app.rag.generator import parse_json_object
from app.retrieval.retriever import Retriever

logger = logging.getLogger(__name__)

_ANAPHORA = re.compile(r"\b(it|its|it's|that|this|those|these|they|them|their|theirs|same|one|ones|there)\b", re.I)
_EXPLETIVE = re.compile(r"\b(is it possible|it is possible|is it ok|is it okay|is that all|that'?s all|"
                        r"this is (?:not|so)|that is (?:not|so))\b", re.I)
_QUESTION_WORD = r"(?!(?:what|how|why|when|where|who|which|can|could|do|does|is|are|will|would)\b)"
_ELLIPSIS = re.compile(r"^\s*(?:and|also|ok(?:ay)?,?|so)?\s*(?:what|how)\s+about\s+(.+?)[?.!]*\s*$"
                       rf"|^\s*(?:and|also)\s+(?:for\s+)?{_QUESTION_WORD}((?:\S+\s*){{1,4}}?)[?.!]*\s*$", re.I)
_SUBJECT_WORDS = ["shopify plus", "shopify", "magento 2", "magento 1", "magento", "bigcommerce", "wordpress",
                  "woocommerce", "drupal", "wix", "squarespace", "x-cart", "xcart", "silver", "gold",
                  "android", "php", "dot net", ".net", "big data", "ios"]

_REWRITE_SYSTEM = (
    "Rewrite the user's latest message as a standalone question that can be understood without the "
    "conversation, using the conversation only to resolve references such as 'it', 'that' or 'what about X'. "
    "Do not answer it and do not add new information. If the message is already standalone or is not a "
    "question about the previous topic, return it unchanged. Reply with JSON only: {\"question\": string}"
)
_REWRITE_SCHEMA = {
    "type": "object",
    "properties": {"question": {"type": "string"}},
    "required": ["question"],
    "additionalProperties": False,
}


class FollowUpRewriter:
    def __init__(self, retriever: Retriever, llm: LLMClient | None = None, domain_threshold: float = 0.52,
                 history_turns: int = 3) -> None:
        self.retriever = retriever
        self.llm = llm
        self.domain_threshold = domain_threshold
        self.history_turns = history_turns

    def _is_candidate(self, message: str) -> bool:
        words = message.split()
        if len(words) > 16 or intent.smalltalk_kind(message) or intent.is_company_overview(message):
            return False
        return bool(_ELLIPSIS.match(message)) or (bool(_ANAPHORA.search(message))
                                                    and not _EXPLETIVE.search(message))

    def rewrite(self, message: str, session: Session) -> str | None:
        """Return a standalone search query, or None to use the message as is."""
        if not session.topic or not self._is_candidate(message):
            return None
        if self.llm is not None:
            rewritten = self._llm_rewrite(message, session)
            if rewritten:
                return rewritten if rewritten.strip().lower() != message.strip().lower() else None
        candidate = self._heuristic(message, session)
        if not candidate:
            return None
        if _ELLIPSIS.match(message):
            return candidate  # a bare "what about X?" is never a standalone question
        original = self.retriever.retrieve(message).top_score
        improved = self.retriever.retrieve(candidate).top_score
        return candidate if improved >= original - 0.02 else None

    def _heuristic(self, message: str, session: Session) -> str | None:
        m = _ELLIPSIS.match(message)
        if m:
            subject = (m.group(1) or m.group(2) or "").strip()
            if not subject:
                return None
            # the new subject must itself be D Group territory ("what about the weather?" is not)
            if self.retriever.retrieve(subject).top_score < self.domain_threshold:
                return None
            prev = session.last_relevant_query or session.topic or ""
            low_prev = prev.lower()
            for word in _SUBJECT_WORDS:
                if word in low_prev:
                    return re.sub(re.escape(word), subject, prev, count=1, flags=re.I)
            return f"{prev} {subject}".strip()
        return f"{session.topic}: {message}"

    def _llm_rewrite(self, message: str, session: Session) -> str | None:
        history = session.llm_history(self.history_turns, max_chars=600)
        convo = "\n".join(f"{h['role']}: {h['content']}" for h in history)
        prompt = f"Conversation:\n{convo}\n\nLatest message: {message}"
        try:
            raw = self.llm.complete(_REWRITE_SYSTEM, [{"role": "user", "content": prompt}], json_schema=_REWRITE_SCHEMA)
            question = str(parse_json_object(raw).get("question", "")).strip()
            return question[:500] or None
        except (LLMError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("follow-up rewrite via LLM failed (%s); using heuristics", exc)
            return None
