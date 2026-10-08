"""Logging setup. Logs go to stderr in a single-line structured format.
User questions are truncated in logs to avoid storing excessive personal data."""
from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        logging.getLogger().setLevel(level.upper())
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "chromadb", "sentence_transformers", "urllib3", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def redact(text: str, limit: int = 80) -> str:
    """Shorten free text before logging it."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
