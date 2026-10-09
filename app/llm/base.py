"""LLM provider abstraction.

Each provider implements `complete(system, messages, json_schema)` and returns
the model's text. Provider SDKs are imported lazily so only the one in use
needs to be installed. API keys come from environment variables only.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Provider call failed (network, auth, rate limit, bad response)."""


class LLMRefusal(LLMError):
    """The model declined to answer (safety refusal)."""


class LLMAuthError(LLMError):
    """Credentials rejected (not transient: retrying every request is pointless)."""


def _is_auth_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    name = type(exc).__name__
    return status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError") or \
        "api key not valid" in str(exc).lower()


class LLMClient(ABC):
    provider: str = "base"
    model: str = ""

    @abstractmethod
    def complete(self, system: str, messages: list[dict[str, str]],
                 json_schema: dict[str, Any] | None = None) -> str:
        """Return the assistant text. `messages` alternate user/assistant."""


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-opus-5-5", max_tokens: int = 1024,
                 timeout: float = 30.0, effort: str = "low", fallbacks: bool = True) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self.model = model
        self._max_tokens = max_tokens
        self._effort = effort
        self._fallbacks = fallbacks

    def complete(self, system, messages, json_schema=None):
        a = self._anthropic
        output_config: dict[str, Any] = {"effort": self._effort}
        if json_schema:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=self._max_tokens, system=system,
            messages=messages, output_config=output_config,
        )
        try:
            if self._fallbacks:
                # Server-side refusal fallback: re-runs a declined request on a
                # fallback model chosen by refusal category.
                resp = self._client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = self._client.messages.create(**kwargs)
        except a.RateLimitError as exc:
            raise LLMError("rate limited by LLM provider") from exc
        except (a.AuthenticationError, a.PermissionDeniedError) as exc:
            raise LLMAuthError("LLM authentication failed - check ANTHROPIC_API_KEY") from exc
        except a.BadRequestError as exc:
            raise LLMError(f"LLM rejected the request: {exc.message}") from exc
        except a.APIStatusError as exc:
            raise LLMError(f"LLM provider error ({exc.status_code})") from exc
        except a.APIConnectionError as exc:
            raise LLMError("could not reach LLM provider") from exc
        if resp.stop_reason == "refusal":
            raise LLMRefusal("model declined the request")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        if not text.strip():
            raise LLMError("empty LLM response")
        return text


class OpenAICompatibleClient(LLMClient):
    """OpenAI and Groq (both expose chat.completions)."""

    def __init__(self, provider: str, api_key: str, model: str, max_tokens: int = 1024,
                 temperature: float = 0.1, timeout: float = 30.0) -> None:
        self.provider = provider
        if provider == "groq":
            from groq import Groq

            self._client = Groq(api_key=api_key, timeout=timeout, max_retries=2)
        else:
            from openai import OpenAI

            self._client = OpenAI(api_key=api_key, timeout=timeout, max_retries=2)
        self.model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

    def complete(self, system, messages, json_schema=None):
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=self._max_tokens, temperature=self._temperature,
            messages=[{"role": "system", "content": system}, *messages],
        )
        if json_schema:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # SDK-specific error classes differ per provider
            if _is_auth_error(exc):
                raise LLMAuthError(f"{self.provider} authentication failed - check its API key") from exc
            raise LLMError(f"{self.provider} call failed: {type(exc).__name__}") from exc
        text = (resp.choices[0].message.content or "") if resp.choices else ""
        if not text.strip():
            raise LLMError("empty LLM response")
        return text


class GeminiClient(LLMClient):
    provider = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash", max_tokens: int = 1024,
                 temperature: float = 0.1) -> None:
        from google import genai
        from google.genai import types

        self._types = types
        self._client = genai.Client(api_key=api_key)
        self.model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

    def complete(self, system, messages, json_schema=None):
        t = self._types
        contents = [
            t.Content(role="model" if m["role"] == "assistant" else "user",
                      parts=[t.Part.from_text(text=m["content"])])
            for m in messages
        ]
        config = t.GenerateContentConfig(
            system_instruction=system, temperature=self._temperature,
            max_output_tokens=self._max_tokens,
            response_mime_type="application/json" if json_schema else None,
        )
        try:
            resp = self._client.models.generate_content(model=self.model, contents=contents, config=config)
        except Exception as exc:
            if _is_auth_error(exc):
                raise LLMAuthError("gemini authentication failed - check GEMINI_API_KEY") from exc
            raise LLMError(f"gemini call failed: {type(exc).__name__}") from exc
        text = resp.text or ""
        if not text.strip():
            raise LLMError("empty LLM response")
        return text


DEFAULT_MODELS = {
    "anthropic": "claude-opus-5-5",
    "groq": "openai/gpt-oss-120b",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
}


class CircuitBreakerLLM(LLMClient):
    """Stops calling a failing provider for a while instead of failing every request.

    * credential errors open the circuit for `auth_cooldown_s` (default 1 h);
    * `failure_threshold` consecutive transient errors open it for `cooldown_s`.
    While open, calls fail fast (no network), so the pipeline answers offline immediately.
    """

    def __init__(self, inner: LLMClient, failure_threshold: int = 5, cooldown_s: float = 60.0,
                 auth_cooldown_s: float = 3600.0, clock=None) -> None:
        import threading
        import time

        self.inner = inner
        self.provider, self.model = inner.provider, inner.model
        self.failure_threshold, self.cooldown_s, self.auth_cooldown_s = failure_threshold, cooldown_s, auth_cooldown_s
        self._clock = clock or time.monotonic
        self._failures = 0
        self._open_until = 0.0
        self.last_error: str | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        return "open" if self._clock() < self._open_until else "closed"

    def complete(self, system, messages, json_schema=None):
        if self._clock() < self._open_until:
            raise LLMError(f"{self.provider} temporarily disabled ({self.last_error})")
        try:
            result = self.inner.complete(system, messages, json_schema)
        except LLMAuthError as exc:
            with self._lock:
                self._open_until = self._clock() + self.auth_cooldown_s
                self.last_error = "authentication failed"
            logger.error("%s rejected the API key; LLM calls paused for %ds (answers continue offline)",
                         self.provider, int(self.auth_cooldown_s))
            raise
        except LLMRefusal:
            raise  # a refusal is about this request, not the provider's health
        except LLMError as exc:
            with self._lock:
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    self._open_until = self._clock() + self.cooldown_s
                    self.last_error = str(exc)[:80]
                    self._failures = 0
                    logger.error("%s failed %d times in a row; LLM calls paused for %ds",
                                 self.provider, self.failure_threshold, int(self.cooldown_s))
            raise
        with self._lock:
            self._failures = 0
        return result


# Expected key formats; in LLM_PROVIDER=auto, keys that clearly don't match are skipped.
KEY_FORMATS = {"anthropic": ("sk-ant-",), "groq": ("gsk_",), "openai": ("sk-",), "gemini": ("AIza",)}


def _looks_valid(provider: str, key: str) -> bool:
    return key.startswith(KEY_FORMATS[provider]) and len(key) >= 20


def create_llm(settings) -> LLMClient | None:
    """Return the configured LLM client (wrapped in a circuit breaker), or None for offline mode."""
    provider = settings.llm_provider
    keys = {
        "anthropic": settings.anthropic_api_key,
        "groq": settings.groq_api_key,
        "openai": settings.openai_api_key,
        "gemini": settings.gemini_api_key or getattr(settings, "google_api_key", None),
    }
    if provider == "extractive":
        return None
    if provider == "auto":
        chosen = None
        for p in ("anthropic", "groq", "openai", "gemini"):
            if not keys[p]:
                continue
            if not _looks_valid(p, keys[p]):
                logger.warning("%s API key does not look like a %s key; skipping it", p.upper(), p)
                continue
            chosen = p
            break
        if chosen is None:
            logger.warning("no usable LLM API key configured; using offline extractive answers")
            return None
        provider = chosen
    key = keys.get(provider)
    if not key:
        raise LLMError(f"LLM_PROVIDER={provider} but its API key is not set")
    if not _looks_valid(provider, key):
        logger.warning("the %s API key does not match the expected format; calls will probably fail", provider)
    model = settings.llm_model or DEFAULT_MODELS[provider]
    logger.info("LLM provider=%s model=%s", provider, model)
    if provider == "anthropic":
        client: LLMClient = AnthropicClient(key, model, settings.llm_max_tokens, settings.llm_timeout_seconds,
                                            settings.llm_effort, settings.anthropic_fallbacks)
    elif provider in ("groq", "openai"):
        client = OpenAICompatibleClient(provider, key, model, settings.llm_max_tokens,
                                        settings.llm_temperature, settings.llm_timeout_seconds)
    else:
        client = GeminiClient(key, model, settings.llm_max_tokens, settings.llm_temperature)
    return CircuitBreakerLLM(client)
