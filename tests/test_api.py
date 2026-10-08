"""FastAPI endpoints and edge cases."""
from __future__ import annotations

import pytest

from tests.conftest import make_client


@pytest.fixture
def client(make_services):
    services = make_services(idle_monitor_enabled=False)
    with make_client(services) as c:
        c.services = services
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["chunks"] > 900 and body["mode"] == "extractive"


def test_ask_answered_with_sources(client):
    r = client.post("/ask", json={"question": "How many support hours are included annually?"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "answered" and "120" in body["answer"]
    assert body["sources"] and body["sources"][0]["url"] == "https://webstore.vgroup.net/services/annual-store-support/"
    assert body["debug"]["top_score"] > 0.55


def test_ask_needs_team_returns_form(client):
    body = client.post("/ask", json={"question": "Can you build an NFT marketplace on blockchain for me?"}).json()
    assert body["status"] == "needs_team"
    assert body["action"]["type"] == "contact_form" and body["action"]["submit_endpoint"] == "/contact"


def test_ask_off_topic(client):
    body = client.post("/ask", json={"question": "Who won the 2022 world cup?"}).json()
    assert body["status"] == "off_topic" and body["off_topic"] is True


def test_ask_overrides(client):
    body = client.post("/ask", json={"question": "What Magento extensions do you offer?", "top_k": 1}).json()
    assert body["status"] == "answered" and len(body["sources"]) == 1
    body = client.post("/ask", json={"question": "Do you build mobile apps?",
                                     "similarity_threshold": 0.99}).json()
    assert body["status"] != "answered"  # top score ~0.84 cannot pass a 0.99 threshold


@pytest.mark.parametrize("payload", [
    {}, {"question": ""}, {"question": "   "}, {"question": "x" * 2001},
    {"question": "hi", "top_k": 0}, {"question": "hi", "top_k": 99},
    {"question": "hi", "similarity_threshold": 1.5}, {"question": 123},
])
def test_ask_validation_errors(client, payload):
    r = client.post("/ask", json=payload)
    assert r.status_code == 422
    assert "Traceback" not in r.text


def test_ask_question_over_configured_limit(client, real_settings):
    r = client.post("/ask", json={"question": "a " * (real_settings.max_question_chars // 2 + 10)})
    assert r.status_code == 422


def test_ask_malformed_json(client):
    r = client.post("/ask", content=b"{bad", headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_contact_valid_and_stored(client):
    r = client.post("/contact", json={"name": "Jane Doe", "email": "jane@example.com", "phone": "+1 609 555 0100",
                                      "message": "I want an NFT marketplace", "request_id": "abc123"})
    assert r.status_code == 201
    body = r.json()
    assert "connect with you shortly" in body["message"] and "anything else" in body["message"]
    lead = client.services.leads.get(body["lead_id"])  # stored in SQLite
    assert lead.email == "jane@example.com" and lead.created_at.endswith("+00:00") and lead.source == "contact_form"


@pytest.mark.parametrize("payload", [
    {"name": "J", "email": "jane@example.com", "message": "hello there"},
    {"name": "Jane", "email": "not-an-email", "message": "hello there"},
    {"name": "Jane", "email": "jane@example.com", "message": "hi"},
    {"name": "Jane", "email": "jane@example.com", "message": "hello there", "phone": "call me"},
])
def test_contact_validation(client, payload):
    assert client.post("/contact", json=payload).status_code == 422


def test_production_hides_debug(make_services):
    services = make_services(idle_monitor_enabled=False, environment="production")
    with make_client(services) as c:
        body = c.post("/ask", json={"question": "What is SMTU?"}).json()
    assert body["debug"] is None
