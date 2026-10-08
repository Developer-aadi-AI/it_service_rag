"""Data models for the knowledge base."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class FAQ:
    question: str
    answer: str


@dataclass
class Document:
    """One cleaned page from the supplied V Group dataset.

    Raw fields (page_id, url, title, text, source_type, platform) come from the
    dataset. The remaining fields are derived from that same content; nothing is
    added from outside the supplied data.
    """

    page_id: str
    url: str
    title: str
    text: str
    source_type: str
    platform: str = ""
    original_source_type: str = ""
    category: str = ""
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    ctas: list[str] = field(default_factory=list)
    platforms_mentioned: list[str] = field(default_factory=list)
    faqs: list[FAQ] = field(default_factory=list)
    content_quality: str = "full"  # full | short | title_only

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Chunk:
    chunk_id: str
    page_id: str
    text: str  # text shown to the LLM / used for citations
    embed_text: str  # text that gets embedded (text + light metadata prefix)
    content_type: str  # page | faq
    metadata: dict[str, Any] = field(default_factory=dict)
