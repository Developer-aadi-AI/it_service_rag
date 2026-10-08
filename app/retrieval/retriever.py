"""Hybrid similarity search with configurable top-k and similarity threshold.

score = dense cosine similarity + lexical_weight * normalised BM25 coverage
(capped at 1.0). Thresholds are applied to this combined score.

Independent of the LLM so it can be tested and evaluated on its own.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from app.embeddings.base import Embedder
from app.retrieval.lexical import BM25Index
from app.vectorstore.base import SearchHit, VectorStore

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    query: str
    hits: list[SearchHit]  # passed the similarity threshold, diversified, <= top_k
    candidates: list[SearchHit] = field(default_factory=list)  # ranked, unfiltered
    top_score: float = 0.0

    @property
    def has_context(self) -> bool:
        return bool(self.hits)


class Retriever:
    def __init__(self, embedder: Embedder, store: VectorStore, top_k: int = 5,
                 similarity_threshold: float = 0.62, candidate_k: int = 20,
                 max_chunks_per_page: int = 2, lexical_weight: float = 0.25) -> None:
        self.embedder = embedder
        self.store = store
        self.top_k = top_k
        self.similarity_threshold = similarity_threshold
        self.candidate_k = max(candidate_k, top_k)
        self.max_chunks_per_page = max_chunks_per_page
        self.lexical_weight = lexical_weight
        self._bm25: BM25Index | None = None
        self._chunk_cache: dict[str, SearchHit] = {}
        self._lock = threading.Lock()

    # -- lexical index -------------------------------------------------------
    def _lexical(self) -> BM25Index | None:
        if self.lexical_weight <= 0:
            return None
        with self._lock:
            if self._bm25 is None:
                chunks = self.store.all_chunks()
                self._chunk_cache = {c.chunk_id: c for c in chunks}
                self._bm25 = BM25Index(
                    [c.chunk_id for c in chunks],
                    [f"{c.metadata.get('title', '')} {c.text}" for c in chunks],
                )
        return self._bm25

    def invalidate(self) -> None:
        """Call after the underlying store changes."""
        with self._lock:
            self._bm25 = None
            self._chunk_cache = {}

    # -- search ----------------------------------------------------------------
    def search(self, query: str, k: int | None = None) -> list[SearchHit]:
        """Ranked candidates with combined scores (no threshold applied)."""
        k = max(k or self.candidate_k, 1)
        q_vec = self.embedder.embed_query(query)
        dense = self.store.query(q_vec, k)
        by_id: dict[str, SearchHit] = {h.chunk_id: h for h in dense}
        dense_scores = {h.chunk_id: h.score for h in dense}

        bm25 = self._lexical()
        lex: dict[str, float] = bm25.scores(query) if bm25 else {}
        if lex:
            lex_top = sorted(lex, key=lex.get, reverse=True)[:k]
            missing = [cid for cid in lex_top if cid not in by_id]
            if missing:
                vecs = self.store.get_embeddings(missing)
                for cid in missing:
                    if cid in vecs and cid in self._chunk_cache:
                        base = self._chunk_cache[cid]
                        dense_scores[cid] = float(np.dot(vecs[cid], q_vec))
                        by_id[cid] = SearchHit(cid, base.text, dict(base.metadata), 0.0)

        results = []
        for cid, hit in by_id.items():
            score = min(1.0, dense_scores[cid] + self.lexical_weight * lex.get(cid, 0.0))
            hit.metadata["dense_score"] = round(dense_scores[cid], 4)
            hit.metadata["lexical_score"] = round(lex.get(cid, 0.0), 4)
            results.append(SearchHit(cid, hit.text, hit.metadata, score))
        results.sort(key=lambda h: h.score, reverse=True)
        return results

    def retrieve(self, query: str, top_k: int | None = None,
                 similarity_threshold: float | None = None,
                 metadata_filter: Callable[[dict], bool] | None = None) -> RetrievalResult:
        """Thresholded, diversified hits. `metadata_filter` restricts results to
        chunks whose metadata passes (searched over a wider candidate pool)."""
        query = (query or "").strip()
        if not query:
            return RetrievalResult(query=query, hits=[])
        k = top_k or self.top_k
        threshold = self.similarity_threshold if similarity_threshold is None else similarity_threshold

        if metadata_filter is None:
            candidates = self.search(query, max(self.candidate_k, k))
        else:
            pool = self.search(query, max(self.candidate_k * 5, 100))
            candidates = [c for c in pool if metadata_filter(c.metadata)]
        top_score = candidates[0].score if candidates else 0.0

        hits: list[SearchHit] = []
        per_page: dict[str, int] = {}
        for hit in candidates:
            if hit.score < threshold:
                break  # sorted by score
            page = hit.metadata.get("page_id", hit.chunk_id)
            if per_page.get(page, 0) >= self.max_chunks_per_page:
                continue
            per_page[page] = per_page.get(page, 0) + 1
            hits.append(hit)
            if len(hits) >= k:
                break
        logger.debug("retrieved %d/%d hits (top=%.3f, thr=%.2f)", len(hits), len(candidates),
                     top_score, threshold)
        return RetrievalResult(query=query, hits=hits, candidates=candidates, top_score=top_score)
