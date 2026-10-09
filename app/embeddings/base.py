"""Embedding providers. All return L2-normalised float32 vectors so that a dot
product equals cosine similarity."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import numpy as np

logger = logging.getLogger(__name__)


def _normalize(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


class Embedder(ABC):
    name: str = "base"

    @property
    @abstractmethod
    def dimension(self) -> int: ...

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    @abstractmethod
    def embed_query(self, text: str) -> np.ndarray: ...


class SentenceTransformerEmbedder(Embedder):
    """Local sentence-transformers model (default: BAAI/bge-base-en-v1.5)."""

    def __init__(self, model_name: str, query_instruction: str = "", batch_size: int = 32,
                 device: str | None = None) -> None:
        from sentence_transformers import SentenceTransformer  # heavy import, keep lazy

        logger.info("loading embedding model %s", model_name)
        self.name = model_name
        self._model = SentenceTransformer(model_name, device=device)
        self._query_instruction = query_instruction
        self._batch_size = batch_size

    @property
    def dimension(self) -> int:
        return int(self._model.get_sentence_embedding_dimension())

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        vecs = self._model.encode(texts, batch_size=self._batch_size, normalize_embeddings=True,
                                  show_progress_bar=False, convert_to_numpy=True)
        return _normalize(vecs)

    def embed_query(self, text: str) -> np.ndarray:
        vec = self._model.encode([self._query_instruction + text], normalize_embeddings=True,
                                 show_progress_bar=False, convert_to_numpy=True)
        return _normalize(vec)[0]


class HashingEmbedder(Embedder):
    """Dependency-light lexical embedder (hashed word/bigram counts).

    Useful for fast unit tests and offline smoke runs; retrieval quality is
    lower than a neural model, so thresholds tuned for bge do not apply.
    """

    name = "hashing"

    def __init__(self, n_features: int = 2048) -> None:
        from sklearn.feature_extraction.text import HashingVectorizer

        self._n = n_features
        self._vec = HashingVectorizer(n_features=n_features, ngram_range=(1, 2),
                                      stop_words="english", alternate_sign=False, norm="l2")

    @property
    def dimension(self) -> int:
        return self._n

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return _normalize(self._vec.transform(texts).toarray())

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([text])[0]


class CachedEmbedder(Embedder):
    """Thread-safe LRU cache in front of another embedder.

    Profiling showed embedding dominates request time: the offline answer
    selector re-embeds the same page sentences for many questions, and one chat
    turn embeds the same query for retrieval, follow-up checks and
    recommendations. Vectors are cached per text (query and document caches
    are separate because queries carry an instruction prefix).
    """

    def __init__(self, inner: Embedder, max_items: int = 8192) -> None:
        import threading
        from collections import OrderedDict

        self.inner = inner
        self.name = inner.name
        self.max_items = max_items
        self._docs: "OrderedDict[str, np.ndarray]" = OrderedDict()
        self._queries: "OrderedDict[str, np.ndarray]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    @property
    def dimension(self) -> int:
        return self.inner.dimension

    def _get(self, cache, key):
        with self._lock:
            vec = cache.get(key)
            if vec is not None:
                cache.move_to_end(key)
                self.hits += 1
            else:
                self.misses += 1
            return vec

    def _put(self, cache, key, vec) -> None:
        with self._lock:
            cache[key] = vec
            cache.move_to_end(key)
            while len(cache) > self.max_items:
                cache.popitem(last=False)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        found = [self._get(self._docs, t) for t in texts]
        missing = [i for i, v in enumerate(found) if v is None]
        if missing:
            new = self.inner.embed_documents([texts[i] for i in missing])
            for i, vec in zip(missing, new):
                found[i] = vec
                self._put(self._docs, texts[i], vec)
        if not texts:
            return np.zeros((0, self.dimension), dtype=np.float32)
        return np.vstack(found).astype(np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        vec = self._get(self._queries, text)
        if vec is None:
            vec = self.inner.embed_query(text)
            self._put(self._queries, text, vec)
        return vec


def create_embedder(settings) -> Embedder:
    if settings.embedding_provider == "hashing":
        base: Embedder = HashingEmbedder()
    else:
        base = SentenceTransformerEmbedder(
            settings.embedding_model,
            query_instruction=settings.embedding_query_instruction,
            batch_size=settings.embedding_batch_size,
            device=settings.embedding_device,
        )
    cache = getattr(settings, "embedding_cache_size", 0)
    return CachedEmbedder(base, cache) if cache else base
