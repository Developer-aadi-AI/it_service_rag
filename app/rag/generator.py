"""Answer generation over retrieved context.

* LLMGenerator      - grounded prompt + JSON output from the configured LLM.
* ExtractiveGenerator - offline fallback: returns the best-supported sentences
  from the retrieved chunks verbatim (grounded by construction).
* Triage            - decides whether an unanswered, in-domain message is a
  project request, an information request, or off-topic.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from app.embeddings.base import Embedder
from app.ingestion.loader import split_sentences
from app.llm.base import LLMClient, LLMError
from app.rag import intent
from app.rag.humanize import clean_sentence, compose_answer, is_fragment, plan_price_sentences
from app.rag.prompts import (GROUNDED_SCHEMA, GROUNDED_SYSTEM_PROMPT, TRIAGE_SCHEMA, TRIAGE_SYSTEM_PROMPT,
                             ContextSource, build_context, build_user_message)
from app.retrieval.lexical import tokenize

logger = logging.getLogger(__name__)

Status = Literal["answered", "needs_team", "off_topic"]
RequestKind = Literal["project_request", "information_request", "other"]


@dataclass
class GenerationResult:
    status: Status
    answer: str = ""
    citations: list[int] = field(default_factory=list)
    request_kind: RequestKind = "other"
    generator: str = ""


_CITATION = re.compile(r"\[(\d{1,2})\]")


def clean_citations(answer: str, valid: set[int]) -> tuple[str, list[int]]:
    """Drop citation markers that point to non-existent sources."""
    used: list[int] = []

    def _sub(m: re.Match) -> str:
        n = int(m.group(1))
        if n in valid:
            if n not in used:
                used.append(n)
            return m.group(0)
        return ""

    cleaned = _CITATION.sub(_sub, answer)
    return re.sub(r"\s+([.,;:])", r"\1", re.sub(r"[ \t]{2,}", " ", cleaned)).strip(), used


def parse_json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


class LLMGenerator:
    name = "llm"

    def __init__(self, llm: LLMClient, max_context_chars: int = 6000) -> None:
        self.llm = llm
        self.max_context_chars = max_context_chars

    def generate(self, question: str, sources: list[ContextSource],
                 history: list[dict[str, str]] | None = None, overview: bool = False) -> GenerationResult:
        context = build_context(sources, self.max_context_chars)
        messages = [*(history or []), {"role": "user", "content": build_user_message(question, context)}]
        raw = self.llm.complete(GROUNDED_SYSTEM_PROMPT, messages, json_schema=GROUNDED_SCHEMA)
        try:
            data = parse_json_object(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMError("LLM returned invalid JSON") from exc

        status = data.get("status")
        if status not in ("answered", "needs_team", "off_topic"):
            raise LLMError(f"LLM returned unknown status {status!r}")
        kind = data.get("request_kind") if data.get("request_kind") in (
            "project_request", "information_request", "other") else "other"
        if status != "answered":
            return GenerationResult(status=status, request_kind=kind, generator=self.name)

        valid = {s.number for s in sources}
        answer, used = clean_citations(str(data.get("answer", "")), valid)
        cited = [n for n in data.get("citations", []) if isinstance(n, int) and n in valid]
        citations = list(dict.fromkeys(used + cited))
        if not answer:
            return GenerationResult(status="needs_team", request_kind=kind, generator=self.name)
        if not citations:
            # Grounding guard: an answer that cites nothing is not trusted.
            logger.warning("LLM answer without citations; routing to team")
            return GenerationResult(status="needs_team", request_kind=kind, generator=self.name)
        return GenerationResult("answered", answer, citations, kind, self.name)


# Prefer authoritative pages over articles when picking evidence offline.
_SOURCE_PRIOR = {"contact": 0.06, "pricing": 0.03, "service": 0.02, "about": 0.02,
                 "blog-post": -0.05, "policy": -0.05, "blog-index": -0.1, "product-category": -0.05}
_ARTICLE_TYPES = {"blog-post", "blog-index", "policy"}
# Words that describe *how* rather than *what*; ignored when checking that the
# evidence covers the subject of the question.
_GENERIC = set("""build built building develop developing development developer create make custom
customize design designing website websites site web store stores app apps application company business
help set setup need want looking new service services offer provide get integrate integration work
working support plan plans team project projects online ecommerce e-commerce solution located location
cost costs price pricing charge charges much did does doing done any there address detail details
about info information""".split())
# Platform names: if the user names one, the evidence must name it too.
_PLATFORMS = set("""shopify magento bigcommerce wordpress woocommerce drupal wix squarespace xcart x-cart
amazon""".split())


_RAW_PLAN_PRICE = re.compile(r"\b(?:Silver|Gold|Platinum|Bronze|Basic|Premium|Starter|Pro)\s+Plan:?\s*[$£]")


def _stem(token: str) -> str:
    """Crude stemming by prefix: include/included/includes -> 'inclu'."""
    return token[:5] if len(token) >= 5 else token


# Stems that count as covering each other in the subject check (same intent, different wording).
_SYNONYM_GROUPS = [
    {"job", "jobs", "caree", "hirin", "vacan", "openi", "posit", "join", "recru"},
    {"start", "found", "began", "begin", "estab", "journ", "since"},
    {"offic", "locat", "addre", "headq"},
    {"clien", "brand", "custo", "colla"},
]


def _expand(stems: set[str]) -> set[str]:
    out = set(stems)
    for group in _SYNONYM_GROUPS:
        if stems & group:
            out |= group
    return out


def _covered(subject: set[str], evidence: set[str]) -> float:
    """Fraction of subject stems found in the evidence (synonyms allowed)."""
    if not subject:
        return 1.0
    ev = _expand(evidence)
    return sum(1 for s in subject if s in ev) / len(subject)


class ExtractiveGenerator:
    """Offline answerer: selects the best-supported sentences verbatim."""

    name = "extractive"

    def __init__(self, embedder: Embedder, min_score: float = 0.62, max_sentences: int = 3,
                 max_sentence_chars: int = 400, min_subject_coverage: float = 0.5) -> None:
        self.embedder = embedder
        self.min_subject_coverage = min_subject_coverage
        self.min_score = min_score
        self.max_sentences = max_sentences
        self.max_sentence_chars = max_sentence_chars

    def _candidates(self, question: str, sources: list[ContextSource],
                    project: bool, overview: bool = False) -> list[tuple[str, str, int, float, int]]:
        """(text_to_score, text_to_show, source_number, prior, position)."""
        q_tokens = set(tokenize(question))
        out: list[tuple[str, str, int, float, int]] = []
        pos = 0
        for src in sources:
            if project and src.source_type in _ARTICLE_TYPES:
                continue  # articles describe topics, not what V Group will build
            prior = _SOURCE_PRIOR.get(src.source_type, 0.0)
            if overview and src.source_type in ("about", "home"):
                prior += 0.08  # company overview: prefer the About/Home pages
            for hit in src.hits:
                meta = hit.metadata
                if src.source_type == "pricing":
                    # Flattened price tables -> plain sentences (prices verbatim from the page).
                    price_sents = plan_price_sentences(hit.text, src.title)
                    for sent in price_sents:
                        out.append((sent, sent, src.number, prior + 0.03, pos))
                        pos += 1
                else:
                    price_sents = []
                hit_prior = prior - (0.1 if meta.get("content_quality") == "title_only" else 0.0)
                if meta.get("content_quality") == "title_only" and not (q_tokens & set(tokenize(src.title))):
                    continue  # placeholder portfolio text only helps when the client is named
                if meta.get("content_type") == "faq":
                    m = re.match(r"Q: (.*?\?) A: (.*)", hit.text, re.S)
                    if m:
                        answer = clean_sentence(m.group(2).strip())
                        out.append((hit.text, answer, src.number, hit_prior + 0.05, pos))
                        pos += 1
                        continue
                for raw in split_sentences(hit.text):
                    if raw.endswith("?"):
                        continue  # questions/headings are not answers
                    if price_sents and _RAW_PLAN_PRICE.search(raw):
                        continue  # raw price-table text; the clean price sentences replace it
                    sent = clean_sentence(raw)
                    if not sent or (meta.get("content_quality") != "title_only" and is_fragment(sent)):
                        continue  # menus, headings, flattened tables
                    # Score with the page title attached: "About V Group. Our journey started in 1999..."
                    out.append((f"{src.title}. {sent}", sent, src.number, hit_prior, pos))
                    pos += 1
        seen: set[str] = set()
        unique = []
        for c in out:
            key = c[1].lower()
            if key not in seen:
                seen.add(key)
                unique.append(c)
        return unique

    def generate(self, question: str, sources: list[ContextSource],
                 history: list[dict[str, str]] | None = None, overview: bool = False) -> GenerationResult:
        project = intent.is_project_request(question) and not overview
        kind: RequestKind = "project_request" if project else "information_request"
        cands = self._candidates(question, sources, project, overview)
        q_tokens = set(tokenize(question))
        # Subject guard: evidence must mention what the user is asking about
        # (e.g. "NFT marketplace" must not be answered with a generic store FAQ).
        subject = {_stem(t) for t in q_tokens if t not in _GENERIC and len(t) > 1}
        if subject and not overview:
            titles = {s.number: s.title for s in sources}
            platforms = {_stem(t) for t in q_tokens if t in _PLATFORMS}

            def _covers(c) -> bool:
                ev = {_stem(t) for t in tokenize(f"{c[0]} {titles.get(c[2], '')}")}
                return platforms <= ev and _covered(subject, ev) >= self.min_subject_coverage

            cands = [c for c in cands if _covers(c)]
        # One-line portfolio stubs only answer when no full page covers the subject.
        stub_sources = {s.number for s in sources
                        if all(h.metadata.get("content_quality") == "title_only" for h in s.hits)}
        if any(c[2] not in stub_sources for c in cands):
            cands = [c for c in cands if c[2] not in stub_sources]
        if overview:  # an overview needs substantive sentences, not taglines
            cands = [c for c in cands if len(c[1].split()) >= 12] or cands
        if not cands:
            return GenerationResult("needs_team", request_kind=kind, generator=self.name)

        vecs = self.embedder.embed_documents([c[0] for c in cands])
        q_vec = self.embedder.embed_query(question)
        coverage = np.array([len(q_tokens & set(tokenize(c[0]))) / max(1, len(q_tokens)) for c in cands])
        priors = np.array([c[3] for c in cands])
        if re.match(r"\s*(when|since when|what year)\b", question, re.I):  # dates answer "when"
            priors = priors + np.array([0.06 if re.search(r"\b(19|20)\d\d\b", c[1]) else 0.0 for c in cands])
        scores = vecs @ q_vec + 0.15 * coverage + priors
        order = np.argsort(-scores)
        best = float(scores[order[0]])
        if best < self.min_score:
            return GenerationResult("needs_team", request_kind=kind, generator=self.name)

        margin = 0.12 if overview else 0.08
        chosen: list[int] = []
        for i in order[: self.max_sentences * 5]:
            if scores[i] < best - margin or len(chosen) >= self.max_sentences:
                break
            if any(float(vecs[i] @ vecs[j]) > 0.88 for j in chosen):
                continue  # near-duplicate of a sentence already chosen
            chosen.append(int(i))

        # Readable order: group by source (best source first), page order within a source.
        if overview:  # lead a company overview with the About/Home pages
            types = {s.number: s.source_type for s in sources}
            chosen.sort(key=lambda i: (types.get(cands[i][2]) != "about", types.get(cands[i][2]) != "home"))
        source_rank: dict[int, int] = {}
        for i in chosen:
            source_rank.setdefault(cands[i][2], len(source_rank))
        chosen.sort(key=lambda i: (source_rank[cands[i][2]], cands[i][4]))
        picked: list[tuple[str, int]] = []
        for i in chosen:
            text = cands[i][1]
            if len(text) > self.max_sentence_chars:
                text = text[: self.max_sentence_chars].rsplit(" ", 1)[0] + "..."
            picked.append((text, cands[i][2]))

        answer = compose_answer(picked, question, "overview" if overview else kind)
        citations = list(dict.fromkeys(n for _, n in picked))
        return GenerationResult("answered", answer, citations, kind, self.name)


class Triage:
    """Classify an in-domain message that the knowledge base could not answer."""

    def __init__(self, llm: LLMClient | None, capabilities: str) -> None:
        self.llm = llm
        self.capabilities = capabilities

    def classify(self, question: str) -> RequestKind | Literal["off_topic"]:
        if self.llm is not None:
            try:
                raw = self.llm.complete(
                    TRIAGE_SYSTEM_PROMPT.format(capabilities=self.capabilities),
                    [{"role": "user", "content": question}], json_schema=TRIAGE_SCHEMA)
                category = parse_json_object(raw).get("category")
                if category in ("project_request", "information_request", "off_topic"):
                    return category
            except (LLMError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("triage LLM failed (%s); using heuristics", exc)
        return "project_request" if intent.is_project_request(question) else "information_request"
