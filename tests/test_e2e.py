"""End-to-end: widget -> FastAPI -> session -> query processing -> retrieval -> LLM -> grounded answer
-> citations -> recommendation -> lead -> transcript -> email -> feedback -> analytics/logging.

Runs the real index and retrieval. The LLM is a scripted fake (deterministic, no network) that
answers from the context it receives, so the LLM path is exercised end to end.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

import pytest

from app.analytics.summary import summarize
from app.llm.base import CircuitBreakerLLM, LLMAuthError, LLMClient, LLMError
from app.rag.generator import ExtractiveGenerator, LLMGenerator
from tests.conftest import make_client


class ContextEchoLLM(LLMClient):
    """Answers with the first sentence of source [1] (proves context reached the model)."""

    provider, model = "fake", "context-echo"

    def __init__(self):
        self.calls = []

    def complete(self, system, messages, json_schema=None):
        self.calls.append(messages[-1]["content"])
        user = messages[-1]["content"]
        if "Latest message:" in user:  # follow-up rewrite request
            return json.dumps({"question": user.rsplit("Latest message:", 1)[1].strip()})
        if "D Group offerings" in system:  # triage
            return json.dumps({"category": "project_request"})
        if "Summarise this customer chat" in system:
            return json.dumps({"points": ["You asked about Shopify package pricing."]})
        m = re.search(r'<source id="1"[^>]*>\n(.*?)\n</source>', user, re.S)
        if not m:
            return json.dumps({"status": "needs_team", "answer": "", "citations": [], "request_kind": "other"})
        first = re.split(r"(?<=[.!?])\s", m.group(1).strip())[0]
        return json.dumps({"status": "answered", "answer": f"{first} [1]", "citations": [1],
                           "request_kind": "information_request"})


@pytest.fixture
def llm_stack(make_services, clock, real_bundle, real_settings):
    """Services whose pipeline uses the fake LLM (restored afterwards)."""
    p = real_bundle.pipeline
    saved = (p.llm, p.generator, p.triage.llm)
    llm = ContextEchoLLM()
    p.llm, p.generator, p.triage.llm = llm, LLMGenerator(llm, real_settings.max_context_chars), llm
    svc = make_services(clock=clock, idle_monitor_enabled=False, team_notification_email="sales@example.com")
    svc.manager.rewriter.llm = llm
    yield svc, llm
    p.llm, p.generator, p.triage.llm = saved


def test_full_customer_journey(llm_stack, clock, caplog):
    svc, llm = llm_stack
    with make_client(svc) as c, caplog.at_level(logging.INFO):
        # widget loads
        page = c.get("/widget/")
        assert page.status_code == 200 and "widget.js" in page.text
        assert c.get("/widget/widget.js").status_code == 200

        # session + grounded LLM answer with citations
        sid = c.post("/sessions", json={"sound_enabled": True}).json()["session"]["session_id"]
        clock.advance(5)
        a = c.post(f"/sessions/{sid}/messages", json={"message": "How much does the Shopify Gold plan cost?"}).json()
        assert a["status"] == "answered" and a["debug"]["generator"] == "llm"
        assert a["sources"][0]["url"].startswith("https://webstore.vgroup.net/")
        assert re.search(r"\[1\]", a["message"]["content"])
        assert "<context>" in llm.calls[0] and "Shopify Gold plan" in llm.calls[0]
        assert a["recommendations"]["primary"]["name"] == "Shopify Packages"
        assert a["notification"]["sound"] == "message_received"

        # follow-up uses memory (history sent to the LLM)
        clock.advance(5)
        b = c.post(f"/sessions/{sid}/messages", json={"message": "And how long does it take to deliver?"}).json()
        assert b["status"] in ("answered", "needs_team")

        # lead capture
        clock.advance(5)
        prompt = c.post(f"/sessions/{sid}/messages", json={"message": "I want to talk to your sales team"}).json()
        assert prompt["action"]["type"] == "contact_form"
        lead = c.post(f"/sessions/{sid}/contact", json={"name": "Ravi Kumar", "email": "ravi@example.com",
                                                        "message": "Shopify Gold package please"}).json()
        assert lead["message"]["content"].startswith("Thank you, Ravi!")

        # transcript
        t = c.get(f"/sessions/{sid}/transcript").text
        assert "How much does the Shopify Gold plan cost?" in t and "(Submitted the contact form)" in t

        # close -> summary email with feedback links -> one-click feedback
        clock.advance(5)
        end = c.post(f"/sessions/{sid}/messages", json={"message": "no thanks"}).json()
        assert end["status"] == "session_end" and end["action"]["type"] == "feedback"
        summary = next(e for e in svc.mailbox.sent if e.email_type == "summary")
        assert summary.to == "ravi@example.com" and summary.attachments
        assert "You asked about Shopify package pricing." in summary.text  # LLM summary
        link = re.search(r"/feedback/([\w-]+)\?rating=Great", summary.text).group(1)
        assert c.get(f"/feedback/{link}", params={"rating": "Great"}).status_code == 200
        team = next(e for e in svc.mailbox.sent if e.email_type == "team_lead")
        assert team.to == "sales@example.com" and "Ravi Kumar" in team.text

    # analytics reflect the journey (and contain no PII)
    s = summarize(svc.db, clock() - timedelta(days=1), clock() + timedelta(seconds=1))
    assert s["sessions"]["started"] == 1 and s["sessions"]["close_reasons"] == {"user_ended": 1}
    assert s["leads"]["submitted"] == 1 and s["feedback"]["ratings"]["Great"] == 1
    assert s["recommendations"]["shown"] >= 1 and s["emails"]["sent"] >= 2
    assert "ravi@example.com" not in json.dumps(svc.db.query("SELECT * FROM events"))

    # logging: request ids, session ids, LLM latency, no customer email
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("llm provider=fake model=context-echo latency_ms=") for m in messages)
    assert any("POST /sessions/" in m and "-> 200" in m for m in messages)
    assert not any("ravi@example.com" in m for m in messages)


def test_llm_failure_falls_back_and_is_recorded(make_services, clock, real_bundle, real_settings, fake_llm_factory):
    p = real_bundle.pipeline
    saved = (p.llm, p.generator)
    broken = fake_llm_factory([LLMError("upstream 500")] * 3)
    p.llm, p.generator = broken, LLMGenerator(broken, real_settings.max_context_chars)
    try:
        svc = make_services(clock=clock, idle_monitor_enabled=False)
        with make_client(svc) as c:
            sid = c.post("/sessions").json()["session"]["session_id"]
            r = c.post(f"/sessions/{sid}/messages", json={"message": "How many support hours are included annually?"}).json()
        assert r["status"] == "answered" and "120" in r["message"]["content"]  # offline fallback answered
        s = summarize(svc.db, clock() - timedelta(days=1), clock() + timedelta(seconds=1))
        assert s["errors"]["by_component"].get("llm") == 1
    finally:
        p.llm, p.generator = saved


def test_vector_store_failure_returns_friendly_error(make_services, clock, real_bundle, monkeypatch):
    svc = make_services(clock=clock, idle_monitor_enabled=False)

    def down(*a, **k):
        raise ConnectionError("vector store unreachable")

    monkeypatch.setattr(real_bundle.pipeline.retriever, "retrieve", down)
    with make_client(svc) as c:
        sid = c.post("/sessions").json()["session"]["session_id"]
        r = c.post(f"/sessions/{sid}/messages", json={"message": "What is SMTU?"}).json()
    assert r["status"] == "error" and r["action"]["type"] == "contact_form"
    assert "unreachable" not in r["message"]["content"]
    s = summarize(svc.db, clock() - timedelta(days=1), clock() + timedelta(seconds=1))
    assert s["errors"]["by_component"].get("rag", 0) >= 1


def test_lead_storage_failure(make_services, clock, monkeypatch):
    svc = make_services(clock=clock, idle_monitor_enabled=False)

    def db_down(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(svc.leads, "create", db_down)
    with make_client(svc) as c:
        sid = c.post("/sessions").json()["session"]["session_id"]
        r = c.post(f"/sessions/{sid}/contact", json={"name": "Ana Silva", "email": "ana@example.com",
                                                     "message": "Please call me"})
        standalone = c.post("/contact", json={"name": "Ana Silva", "email": "ana@example.com", "message": "Please call me"})
    assert r.status_code == 503 and "couldn't save your details" in r.json()["detail"]
    assert standalone.status_code == 503 and "disk" not in standalone.text
    s = summarize(svc.db, clock() - timedelta(days=1), clock() + timedelta(seconds=1))
    assert s["errors"]["by_component"].get("leads") == 2


def test_health_readiness(make_services):
    svc = make_services(idle_monitor_enabled=False)
    with make_client(svc) as c:
        h = c.get("/health").json()
        assert h["status"] == "ok" and h["checks"]["vector_index"] == "ok" and h["checks"]["database"] == "ok"
        assert c.get("/health/live").json() == {"status": "ok"}
        assert c.get("/health/ready").json()["status"] == "ready"
        svc.db.close()  # simulate a broken database
        assert c.get("/health/ready").status_code == 503
        assert c.get("/health").json()["status"] == "degraded"


# --- circuit breaker --------------------------------------------------------------------------

class _Scripted(LLMClient):
    provider, model = "fake", "x"

    def __init__(self, items):
        self.items, self.calls = list(items), 0

    def complete(self, system, messages, json_schema=None):
        self.calls += 1
        item = self.items.pop(0) if self.items else "ok"
        if isinstance(item, Exception):
            raise item
        return item


def test_circuit_breaker_opens_on_auth_error():
    now = [0.0]
    inner = _Scripted([LLMAuthError("bad key")])
    cb = CircuitBreakerLLM(inner, auth_cooldown_s=100, clock=lambda: now[0])
    with pytest.raises(LLMAuthError):
        cb.complete("s", [])
    with pytest.raises(LLMError):
        cb.complete("s", [])  # fails fast, no network call
    assert inner.calls == 1 and cb.state == "open"
    now[0] = 101
    assert cb.complete("s", []) == "ok" and cb.state == "closed"


def test_circuit_breaker_opens_after_repeated_failures():
    now = [0.0]
    inner = _Scripted([LLMError("500")] * 3)
    cb = CircuitBreakerLLM(inner, failure_threshold=3, cooldown_s=30, clock=lambda: now[0])
    for _ in range(3):
        with pytest.raises(LLMError):
            cb.complete("s", [])
    with pytest.raises(LLMError):
        cb.complete("s", [])
    assert inner.calls == 3
    now[0] = 31
    assert cb.complete("s", []) == "ok"


def test_auto_provider_skips_malformed_keys(real_settings, caplog):
    from app.llm.base import create_llm

    s = real_settings.model_copy(update={"llm_provider": "auto", "anthropic_api_key": "not-a-real-key-123456",
                                         "groq_api_key": None, "openai_api_key": None, "gemini_api_key": None,
                                         "google_api_key": None})
    with caplog.at_level(logging.WARNING):
        assert create_llm(s) is None
    assert any("does not look like" in r.getMessage() for r in caplog.records)
    assert not any("not-a-real-key" in r.getMessage() for r in caplog.records)
