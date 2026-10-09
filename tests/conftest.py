"""Shared fixtures.

* `real_bundle`: the real dataset + bge embeddings + persistent index, offline
  extractive mode (no API keys needed). Session-scoped: built/loaded once.
* `tiny_settings` / synthetic records: fast unit tests with the hashing embedder.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("HF_HUB_OFFLINE", "1")  # use the cached embedding model
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.llm.base import LLMClient  # noqa: E402

SYNTHETIC = [
    {"page_id": "p1", "url": "https://example.test/services/shopify/", "title": "Shopify",
     "source_type": "service", "platform": "",
     "text": "We build custom Shopify stores for growing brands. Our Shopify team handles theme setup, "
             "app integration and migrations. Your Questions, Answered We've answered what most of our "
             "clients ask. Contact us 1. Can you set up my Shopify store? Yes. We set up stores from "
             "scratch including themes and apps. 2. Do you offer support after launch? Yes, we offer "
             "annual support plans for every store."},
    {"page_id": "p2", "url": "https://example.test/pricing/", "title": "Packages", "source_type": "pricing",
     "platform": "", "text": "Silver Plan costs $3,999 and the Gold Plan costs $5,999. Silver is delivered "
                             "in 15 business days. Gold is delivered in 25 business days."},
    {"page_id": "p3", "url": "https://example.test/contact-us/", "title": "Contact Us", "source_type": "UNKNOWN",
     "platform": "", "text": "Email: hello@example.test Phone: 555-0100. Our office is in Springfield."},
    {"page_id": "p4", "url": "https://example.test/portfolio/", "title": "Acme Shoes",
     "source_type": "case-study", "platform": "Shopify CMS", "text": "Acme Shoes"},
    {"page_id": "p5", "url": "https://example.test/preview/", "title": "Preview -", "source_type": "UNKNOWN",
     "platform": "", "text": "Sorry, you are not allowed to access this page."},
    {"page_id": "p1", "url": "https://example.test/dup/", "title": "Duplicate id", "source_type": "about",
     "platform": "", "text": "This record reuses an existing page id and must get a unique id."},
    {"page_id": "bad"},  # missing fields
]


@pytest.fixture
def synthetic_records() -> list[dict]:
    return json.loads(json.dumps(SYNTHETIC))


@pytest.fixture
def tiny_settings(tmp_path, synthetic_records) -> Settings:
    data = tmp_path / "data.json"
    data.write_text(json.dumps(synthetic_records), encoding="utf-8")
    return Settings(_env_file=None, data_path=data, storage_dir=tmp_path / "storage",
                    embedding_provider="hashing", vector_store="numpy", llm_provider="extractive",
                    similarity_threshold=0.2, domain_threshold=0.1, extractive_min_score=0.2,
                    chunk_size=300, chunk_overlap=60, environment="test")


@pytest.fixture(scope="session")
def real_settings() -> Settings:
    return Settings(_env_file=None, llm_provider="extractive", environment="test")


@pytest.fixture(scope="session")
def real_bundle(real_settings):
    from app.rag.pipeline import build_pipeline

    return build_pipeline(real_settings, llm=None)


@pytest.fixture(scope="session")
def eval_set() -> dict:
    from app.evaluation import load_eval_set

    return load_eval_set()


class FakeLLM(LLMClient):
    """Scripted LLM: returns queued responses (str) or raises queued exceptions."""

    provider = "fake"
    model = "fake-1"

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, system, messages, json_schema=None):
        self.calls.append({"system": system, "messages": messages, "schema": json_schema})
        item = self.responses.pop(0) if self.responses else '{"status":"needs_team","answer":"","citations":[],"request_kind":"other"}'
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_llm_factory():
    return FakeLLM


# ============================ Phase 2 fixtures ============================

class FakeClock:
    """Controllable UTC clock for idle/timestamp tests."""

    def __init__(self, start=None):
        from datetime import datetime, timezone

        self.now = start or datetime(2026, 10, 8, 9, 30, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_services(real_bundle, real_settings, tmp_path):
    """Build Phase 2 services on the real index with temp storage and an in-memory mailbox."""
    from app.email.senders import MemoryEmailSender
    from app.services import build_services

    built = []

    def factory(clock=None, **overrides):
        settings = real_settings.model_copy(update={"storage_dir": tmp_path / f"s{len(built)}",
                                                    "app_secret_key": "test-secret", **overrides})
        mailbox = MemoryEmailSender()
        kwargs = {"clock": clock} if clock else {}
        svc = build_services(settings, real_bundle, email_sender=mailbox, background_email=False, **kwargs)
        svc.mailbox = mailbox
        built.append(svc)
        return svc

    yield factory
    for svc in built:
        svc.shutdown()


def make_client(services, settings_override=None):
    """TestClient around an app with pre-built services (no startup build, no idle monitor)."""
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import create_app

    app = create_app(settings_override or services.settings)
    app.state.services = services
    app.state.owns_services = False
    app.dependency_overrides[get_settings] = lambda: settings_override or services.settings
    return TestClient(app, raise_server_exceptions=False)
