"""Centralised configuration. Every tunable value lives here and can be
overridden with an environment variable (or a `.env` file)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- App -----------------------------------------------------------
    app_name: str = "V Group AI Assistant"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    cors_origins: str = "*"  # comma separated

    # --- Data ----------------------------------------------------------
    data_path: Path = PROJECT_ROOT / "Database" / "final_dataset.json"
    storage_dir: Path = PROJECT_ROOT / "storage"

    # --- Chunking ------------------------------------------------------
    chunk_size: int = Field(900, ge=200, le=4000, description="target characters per chunk")
    chunk_overlap: int = Field(150, ge=0, le=1000, description="approx. overlap characters")
    min_chunk_chars: int = Field(40, ge=1)

    # --- Embeddings ----------------------------------------------------
    embedding_provider: Literal["sentence-transformers", "hashing"] = "sentence-transformers"
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    embedding_query_instruction: str = (
        "Represent this sentence for searching relevant passages: "
    )
    embedding_batch_size: int = 32
    embedding_device: str | None = None  # e.g. "cpu", "cuda"

    # --- Vector store --------------------------------------------------
    vector_store: Literal["chroma", "numpy"] = "chroma"
    collection_name: str = "vgroup_knowledge"
    rebuild_index_on_change: bool = True

    # --- Retrieval -----------------------------------------------------
    top_k: int = Field(5, ge=1, le=20)
    candidate_k: int = Field(20, ge=1, le=100, description="candidates fetched before filtering")
    # Thresholds below were calibrated on the supplied data with bge-base-en-v1.5
    # (see README "Threshold calibration"). Re-calibrate if the embedding model changes.
    # Hybrid score (0..1) at/above which a chunk is trusted as answer context.
    similarity_threshold: float = Field(0.55, ge=0.0, le=1.0)
    # Below similarity_threshold but at/above this: the question is in V Group's
    # domain, but the knowledge base has no specific answer -> route to the team.
    domain_threshold: float = Field(0.52, ge=0.0, le=1.0)
    max_chunks_per_page: int = Field(2, ge=1, le=10)
    max_context_chars: int = Field(6000, ge=500)
    # Weight of normalised BM25 term coverage added to the dense cosine score.
    lexical_weight: float = Field(0.25, ge=0.0, le=1.0)
    # Offline extractive mode: minimum sentence evidence score to answer.
    extractive_min_score: float = Field(0.62, ge=0.0, le=2.0)

    # --- LLM -----------------------------------------------------------
    llm_provider: Literal["auto", "anthropic", "groq", "openai", "gemini", "extractive"] = "auto"
    llm_model: str | None = None  # provider default when empty
    llm_temperature: float = 0.1  # ignored by providers that reject sampling params
    llm_max_tokens: int = 1024
    llm_timeout_seconds: float = 30.0
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"  # Anthropic only
    anthropic_fallbacks: bool = True  # server-side refusal fallback (Anthropic only)

    anthropic_api_key: str | None = None
    groq_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None

    # --- Input limits --------------------------------------------------
    max_question_chars: int = Field(1000, ge=10)
    max_history_turns: int = Field(6, ge=0, le=50)

    # --- Business contact surfaced by no-answer flow -------------------
    # Taken from the supplied Contact Us page (db-96). Override if it changes.
    contact_page_url: str = "https://webstore.vgroup.net/contact-us/"

    # ======================= Phase 2: conversation ========================
    # --- Sessions / memory ---
    session_history_turns: int = Field(6, ge=0, le=50, description="user+assistant pairs sent to the LLM")
    session_max_stored_messages: int = Field(200, ge=10, description="cap on messages kept per session")
    session_retention_seconds: int = Field(24 * 3600, ge=60, description="drop sessions from memory after")
    max_message_chars: int = Field(1000, ge=10)

    # --- Irrelevant-input protection ---
    irrelevant_limit: int = Field(3, ge=1, le=20, description="irrelevant inputs before the session ends")
    irrelevant_reset_on_relevant: bool = False  # True: count consecutive irrelevant inputs only

    # --- Idle sessions (seconds) ---
    idle_check_after_seconds: int = Field(120, ge=5, description="inactivity before 'are you still there?'")
    idle_close_after_seconds: int = Field(60, ge=5, description="wait after the check before closing")
    idle_sweep_interval_seconds: int = Field(10, ge=1, description="background idle monitor interval")
    idle_monitor_enabled: bool = True

    # --- Recommendations ---
    recommendations_enabled: bool = True
    recommendation_min_similarity: float = Field(0.60, ge=0.0, le=1.0)
    recommendation_related_count: int = Field(2, ge=0, le=5)

    # --- Sentiment ---
    sentiment_escalate_after: int = Field(2, ge=1, description="consecutive frustrated turns before offering the team")

    # --- Storage ---
    database_path: Path | None = None  # default: <storage_dir>/vgroup.db
    transcripts_dir: Path | None = None  # default: <storage_dir>/transcripts
    save_transcripts_on_close: bool = True

    # --- Email ---
    email_backend: Literal["file", "smtp", "disabled"] = "file"  # file = write .eml to the outbox (dev)
    email_outbox_dir: Path | None = None  # default: <storage_dir>/outbox
    email_from: str = "V Group <no-reply@example.com>"
    email_reply_to: str | None = None
    team_notification_email: str | None = None  # internal address notified of new leads
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    smtp_timeout_seconds: float = 15.0
    email_on_close: bool = True  # summary (+ transcript) and feedback request when a chat ends
    email_attach_transcript: bool = True
    email_on_idle_close: bool = True  # follow-up email when a chat ends for inactivity
    public_base_url: str = "http://127.0.0.1:8000"  # used to build feedback links in emails
    app_secret_key: str | None = None  # signs feedback links; random per process when unset

    @property
    def db_path(self) -> Path:
        return self.database_path or self.storage_dir / "vgroup.db"

    @property
    def transcripts_path(self) -> Path:
        return self.transcripts_dir or self.storage_dir / "transcripts"

    @property
    def outbox_path(self) -> Path:
        return self.email_outbox_dir or self.storage_dir / "outbox"

    @model_validator(mode="after")
    def _check_thresholds(self) -> "Settings":
        if self.email_backend == "smtp" and not self.smtp_host:
            raise ValueError("EMAIL_BACKEND=smtp requires SMTP_HOST")
        if self.domain_threshold > self.similarity_threshold:
            raise ValueError("domain_threshold must be <= similarity_threshold")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        if self.candidate_k < self.top_k:
            self.candidate_k = self.top_k
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
