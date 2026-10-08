"""Sentence-aware chunking with metadata propagation.

The supplied page text is a single flattened line per page (no paragraph
breaks), so chunks are built by packing sentences up to `chunk_size`
characters, carrying trailing sentences forward as overlap. FAQ pairs are
emitted as their own small chunks because they match user questions well.
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.ingestion.loader import split_sentences
from app.ingestion.models import Chunk, Document


def _window_split(sentence: str, size: int) -> list[str]:
    """Split an over-long 'sentence' (e.g. a flattened feature table) by words."""
    words, parts, cur = sentence.split(), [], []
    length = 0
    for w in words:
        if cur and length + len(w) + 1 > size:
            parts.append(" ".join(cur))
            cur, length = [], 0
        cur.append(w)
        length += len(w) + 1
    if cur:
        parts.append(" ".join(cur))
    return parts


def chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    sentences: list[str] = []
    for s in split_sentences(text):
        sentences.extend(_window_split(s, chunk_size) if len(s) > chunk_size else [s])

    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for s in sentences:
        if current and length + len(s) + 1 > chunk_size:
            chunks.append(" ".join(current))
            # carry trailing sentences as overlap
            overlap: list[str] = []
            olen = 0
            for prev in reversed(current):
                if olen + len(prev) + 1 > chunk_overlap:
                    break
                overlap.insert(0, prev)
                olen += len(prev) + 1
            current, length = overlap, olen
        current.append(s)
        length += len(s) + 1
    if current:
        tail = " ".join(current)
        if not chunks or tail not in chunks[-1]:
            chunks.append(tail)
    return chunks


def doc_metadata(doc: Document) -> dict[str, Any]:
    """Flat, scalar-only metadata (vector stores such as Chroma require it)."""
    return {
        "page_id": doc.page_id,
        "url": doc.url,
        "title": doc.title,
        "source_type": doc.source_type,
        "platform": doc.platform,
        "category": doc.category,
        "description": doc.description,
        "keywords": ", ".join(doc.keywords),
        "ctas": " | ".join(doc.ctas),
        "platforms_mentioned": ", ".join(doc.platforms_mentioned),
        "content_quality": doc.content_quality,
    }


def _prefix(doc: Document) -> str:
    bits = [doc.source_type.replace("-", " ")]
    if doc.platform:
        bits.append(doc.platform)
    return f"{doc.title} ({', '.join(bits)}). "


def chunk_documents(
    docs: list[Document], chunk_size: int = 900, chunk_overlap: int = 150, min_chunk_chars: int = 40
) -> list[Chunk]:
    chunks: list[Chunk] = []
    seen_hashes: set[str] = set()

    def _add(chunk: Chunk) -> None:
        digest = hashlib.sha1(chunk.text.lower().encode("utf-8")).hexdigest()
        if digest in seen_hashes:  # exact duplicate content across pages
            return
        seen_hashes.add(digest)
        chunks.append(chunk)

    for doc in docs:
        meta = doc_metadata(doc)
        pieces = chunk_text(doc.text, chunk_size, chunk_overlap)
        for i, piece in enumerate(pieces):
            if len(piece) < min_chunk_chars and doc.content_quality != "title_only":
                continue
            _add(Chunk(
                chunk_id=f"{doc.page_id}::c{i}",
                page_id=doc.page_id,
                text=piece,
                embed_text=_prefix(doc) + piece,
                content_type="page",
                metadata={**meta, "content_type": "page", "chunk_index": i},
            ))
        for j, faq in enumerate(doc.faqs):
            text = f"Q: {faq.question} A: {faq.answer}"
            _add(Chunk(
                chunk_id=f"{doc.page_id}::faq{j}",
                page_id=doc.page_id,
                text=text,
                embed_text=f"{doc.title} FAQ. {text}",
                content_type="faq",
                metadata={**meta, "content_type": "faq", "chunk_index": j},
            ))
    return chunks
