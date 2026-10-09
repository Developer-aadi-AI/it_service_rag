"""Logging setup: request/session correlation, text or JSON output, secret + PII redaction.

Every log record gets `request_id` and `session_id` (from context variables set by the
request middleware). A redaction filter scrubs credentials (configured secret values,
API-key-like tokens, password=...) and masks emails/phone numbers before anything is
written, so free text from users cannot leak contact details into logs.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("session_id", default="-")

_SECRET_PATTERNS = [
    re.compile(r"\b(sk-(?:ant-)?[A-Za-z0-9_\-]{16,})"),        # Anthropic / OpenAI style keys
    re.compile(r"\b(gsk_[A-Za-z0-9]{16,})"),                     # Groq
    re.compile(r"\b(AIza[0-9A-Za-z_\-]{20,})"),                  # Google
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)\b((?:password|passwd|pwd|secret|api[_-]?key|token)\s*[=:]\s*)\S+"),
]
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]*@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
_PHONE = re.compile(r"(?<![\w$])(?:\+?\d[\d\s().\-]{7,}\d)(?![\w])")

_extra_secrets: list[str] = []


def register_secrets(*values: str | None) -> None:
    """Exact secret values (API keys, SMTP password...) to scrub from every log line."""
    for v in values:
        if v and len(v) >= 6 and v not in _extra_secrets:
            _extra_secrets.append(v)


def scrub_pii(text: str) -> str:
    """Mask emails and phone numbers in free text."""
    text = _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
    return _PHONE.sub("[phone]", text)


def scrub_secrets(text: str) -> str:
    for secret in _extra_secrets:
        text = text.replace(secret, "***")
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.lastindex and m.group(0) != m.group(1) else "") + "***", text)
    return text


def redact(text: str, limit: int = 80) -> str:
    """Shorten user free text and mask contact details before logging it."""
    text = scrub_pii(" ".join((text or "").split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ContextFilter(logging.Filter):
    """Adds request/session ids and scrubs secrets from the final message."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.session_id = session_id_var.get()
        try:
            message = record.getMessage()
        except Exception:  # malformed format args: keep the record, don't crash logging
            message = str(record.msg)
        clean = scrub_secrets(message)
        if clean != message or record.args:
            record.msg, record.args = clean, None
        if record.exc_info and not record.exc_text:
            record.exc_text = scrub_secrets(logging.Formatter().formatException(record.exc_info))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "session_id": getattr(record, "session_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_text:
            payload["exception"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False)


_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [req=%(request_id)s sess=%(session_id)s] | %(message)s"
_CONFIGURED = False


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    global _CONFIGURED
    root = logging.getLogger()
    root.setLevel(level.upper())
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(_TEXT_FORMAT, "%Y-%m-%dT%H:%M:%S%z"))
    root.handlers[:] = [handler]
    for noisy in ("httpx", "httpx2", "chromadb", "sentence_transformers", "urllib3", "huggingface_hub",
                  "multipart", "gradio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True
