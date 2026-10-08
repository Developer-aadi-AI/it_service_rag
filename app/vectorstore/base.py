"""Vector store interface plus Chroma and NumPy implementations."""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from app.ingestion.models import Chunk

logger = logging.getLogger(__name__)


@dataclass
class SearchHit:
    chunk_id: str
    text: str
    metadata: dict[str, Any]
    score: float  # cosine similarity, higher is better


class VectorStore(ABC):
    @abstractmethod
    def add(self, chunks: list[Chunk], embeddings: np.ndarray) -> None: ...

    @abstractmethod
    def query(self, embedding: np.ndarray, k: int) -> list[SearchHit]: ...

    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def reset(self) -> None: ...

    @abstractmethod
    def all_chunks(self) -> list[SearchHit]: ...

    @abstractmethod
    def get_embeddings(self, ids: list[str]) -> dict[str, np.ndarray]: ...


class NumpyVectorStore(VectorStore):
    """Exact (brute-force) cosine search. Persisted as .npy + .json.

    For a knowledge base of this size (~1k chunks) exact search is fast and
    has no approximation error, which makes it ideal for tests too.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory
        self._ids: list[str] = []
        self._texts: list[str] = []
        self._metas: list[dict[str, Any]] = []
        self._matrix = np.zeros((0, 0), dtype=np.float32)
        if directory is not None:
            self._load()

    def _paths(self) -> tuple[Path, Path]:
        assert self._dir is not None
        return self._dir / "vectors.npy", self._dir / "chunks.json"

    def _load(self) -> None:
        vec_path, meta_path = self._paths()
        if vec_path.exists() and meta_path.exists():
            self._matrix = np.load(vec_path)
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            self._ids = [d["id"] for d in data]
            self._texts = [d["text"] for d in data]
            self._metas = [d["metadata"] for d in data]

    def _save(self) -> None:
        if self._dir is None:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        vec_path, meta_path = self._paths()
        np.save(vec_path, self._matrix)
        meta_path.write_text(json.dumps(
            [{"id": i, "text": t, "metadata": m} for i, t, m in zip(self._ids, self._texts, self._metas)],
            ensure_ascii=False), encoding="utf-8")

    def add(self, chunks: list[Chunk], embeddings: np.ndarray) -> None:
        embeddings = np.asarray(embeddings, dtype=np.float32)
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings length mismatch")
        self._matrix = embeddings if self._matrix.size == 0 else np.vstack([self._matrix, embeddings])
        self._ids += [c.chunk_id for c in chunks]
        self._texts += [c.text for c in chunks]
        self._metas += [c.metadata for c in chunks]
        self._save()

    def query(self, embedding: np.ndarray, k: int) -> list[SearchHit]:
        if not self._ids:
            return []
        scores = self._matrix @ np.asarray(embedding, dtype=np.float32)
        k = min(k, len(self._ids))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [SearchHit(self._ids[i], self._texts[i], dict(self._metas[i]), float(scores[i])) for i in top]

    def count(self) -> int:
        return len(self._ids)

    def reset(self) -> None:
        self._ids, self._texts, self._metas = [], [], []
        self._matrix = np.zeros((0, 0), dtype=np.float32)
        if self._dir is not None:
            for p in self._paths():
                p.unlink(missing_ok=True)

    def all_chunks(self) -> list[SearchHit]:
        return [SearchHit(i, t, dict(m), 0.0) for i, t, m in zip(self._ids, self._texts, self._metas)]

    def get_embeddings(self, ids: list[str]) -> dict[str, np.ndarray]:
        pos = {cid: i for i, cid in enumerate(self._ids)}
        return {cid: self._matrix[pos[cid]] for cid in ids if cid in pos}


class ChromaVectorStore(VectorStore):
    """Persistent Chroma collection using cosine distance and our own embeddings."""

    def __init__(self, directory: Path, collection_name: str) -> None:
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        self._client = chromadb.PersistentClient(
            path=str(directory), settings=ChromaSettings(anonymized_telemetry=False)
        )
        self._name = collection_name
        self._collection = self._get_collection()

    def _get_collection(self):
        return self._client.get_or_create_collection(
            self._name, metadata={"hnsw:space": "cosine"}, embedding_function=None
        )

    def add(self, chunks: list[Chunk], embeddings: np.ndarray) -> None:
        batch = 500
        for start in range(0, len(chunks), batch):
            part = chunks[start:start + batch]
            self._collection.add(
                ids=[c.chunk_id for c in part],
                documents=[c.text for c in part],
                metadatas=[c.metadata for c in part],
                embeddings=np.asarray(embeddings[start:start + batch], dtype=np.float32).tolist(),
            )

    def query(self, embedding: np.ndarray, k: int) -> list[SearchHit]:
        n = self.count()
        if n == 0:
            return []
        res = self._collection.query(
            query_embeddings=[np.asarray(embedding, dtype=np.float32).tolist()],
            n_results=min(k, n), include=["documents", "metadatas", "distances"],
        )
        hits = []
        for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0],
                                        res["metadatas"][0], res["distances"][0]):
            hits.append(SearchHit(cid, doc, dict(meta or {}), float(1.0 - dist)))
        return hits

    def count(self) -> int:
        return int(self._collection.count())

    def reset(self) -> None:
        try:
            self._client.delete_collection(self._name)
        except Exception:  # collection may not exist
            pass
        self._collection = self._get_collection()

    def all_chunks(self) -> list[SearchHit]:
        res = self._collection.get(include=["documents", "metadatas"])
        return [SearchHit(i, d, dict(m or {}), 0.0)
                for i, d, m in zip(res["ids"], res["documents"], res["metadatas"])]

    def get_embeddings(self, ids: list[str]) -> dict[str, np.ndarray]:
        if not ids:
            return {}
        res = self._collection.get(ids=ids, include=["embeddings"])
        return {cid: np.asarray(vec, dtype=np.float32) for cid, vec in zip(res["ids"], res["embeddings"])}


def create_vector_store(settings, in_memory: bool = False) -> VectorStore:
    if settings.vector_store == "numpy" or in_memory:
        return NumpyVectorStore(None if in_memory else settings.storage_dir / "numpy_index")
    return ChromaVectorStore(settings.storage_dir / "chroma", settings.collection_name)
