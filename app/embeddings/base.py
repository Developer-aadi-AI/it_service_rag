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


def create_embedder(settings) -> Embedder:
    if settings.embedding_provider == "hashing":
        return HashingEmbedder()
    return SentenceTransformerEmbedder(
        settings.embedding_model,
        query_instruction=settings.embedding_query_instruction,
        batch_size=settings.embedding_batch_size,
        device=settings.embedding_device,
    )
