"""Build (or reuse) the vector index from the supplied dataset.

Run directly to (re)build:  python -m app.ingestion.pipeline [--force]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.embeddings.base import Embedder, create_embedder
from app.ingestion.chunker import chunk_documents
from app.ingestion.loader import load_documents
from app.ingestion.models import Chunk, Document
from app.logging_config import setup_logging
from app.vectorstore.base import VectorStore, create_vector_store

logger = logging.getLogger(__name__)

PIPELINE_VERSION = "2"  # bump when cleaning/chunking logic changes


@dataclass
class IndexReport:
    documents: int
    chunks: int
    rebuilt: bool
    seconds: float
    fingerprint: str


def fingerprint(settings: Settings, embedder_name: str) -> str:
    h = hashlib.sha256()
    h.update(settings.data_path.read_bytes())
    h.update(json.dumps({
        "v": PIPELINE_VERSION, "model": embedder_name, "size": settings.chunk_size,
        "overlap": settings.chunk_overlap, "min": settings.min_chunk_chars,
    }, sort_keys=True).encode())
    return h.hexdigest()[:16]


def build_chunks(settings: Settings) -> tuple[list[Document], list[Chunk]]:
    docs = load_documents(settings.data_path)
    chunks = chunk_documents(docs, settings.chunk_size, settings.chunk_overlap, settings.min_chunk_chars)
    return docs, chunks


def ensure_index(settings: Settings, embedder: Embedder, store: VectorStore,
                 force: bool = False) -> IndexReport:
    """Reuse the stored index when the data + settings are unchanged."""
    start = time.perf_counter()
    fp = fingerprint(settings, embedder.name)
    manifest_path = settings.storage_dir / f"manifest_{settings.vector_store}.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}

    up_to_date = manifest.get("fingerprint") == fp and store.count() == manifest.get("chunks")
    if up_to_date and not force:
        logger.info("index up to date (%s chunks, fp=%s)", store.count(), fp)
        return IndexReport(manifest.get("documents", 0), store.count(), False,
                           time.perf_counter() - start, fp)
    if not up_to_date and not force and not settings.rebuild_index_on_change and store.count():
        logger.warning("index is stale but rebuild_index_on_change=false; using existing index")
        return IndexReport(manifest.get("documents", 0), store.count(), False,
                           time.perf_counter() - start, fp)

    docs, chunks = build_chunks(settings)
    logger.info("embedding %d chunks from %d documents", len(chunks), len(docs))
    vectors = embedder.embed_documents([c.embed_text for c in chunks])
    store.reset()
    store.add(chunks, vectors)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({
        "fingerprint": fp, "documents": len(docs), "chunks": len(chunks),
        "embedding_model": embedder.name, "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2), encoding="utf-8")
    elapsed = time.perf_counter() - start
    logger.info("index built in %.1fs", elapsed)
    return IndexReport(len(docs), len(chunks), True, elapsed, fp)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the D Group vector index")
    parser.add_argument("--force", action="store_true", help="rebuild even if unchanged")
    args = parser.parse_args()
    settings = get_settings()
    setup_logging(settings.log_level)
    report = ensure_index(settings, create_embedder(settings), create_vector_store(settings), args.force)
    print(json.dumps(report.__dict__, indent=2))


if __name__ == "__main__":
    main()
