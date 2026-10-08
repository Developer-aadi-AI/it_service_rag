"""Phase 2 HTTP API: sessions, messages, polling, contact, feedback, transcript, email."""
from __future__ import annotations

import re

import pytest

from tests.conftest import make_client


@pytest.fixture
def api(make_services, clock):
    services = make_services(clock=clock, idle_monitor_enabled=False)
    with make_client(services) as c:
        c.services, c.clock = services, clock
        yield c


def new_session(api, **body) -> str:
    r = api.post("/sessions", json=body or None)
    assert r.status_code == 201, r.text
    return r.json()["session"]["session_id"]


def send(api, sid, text):
    api.clock.advance(5)
    return api.post(f"/sessions/{sid}/messages", json={"message": text})


def test_create_session_returns_greeting_and_config(api):
    r = api.post("/sessions", json={"name": "Priya", "email": "priya@example.com"})
    body = r.json()
    assert r.status_code == 201
    assert body["message"]["kind"] == "greeting" and body["message"]["role"] == "assistant"
    s = body["session"]
    assert s["status"] == "active" and s["attempts_remaining"] == 3 and s["irrelevant_limit"] == 3
    assert s["idle_check_after_seconds"] == 120 and s["idle_close_after_seconds"] == 60 and s["sound_enabled"]
    assert body["message"]["created_at"].endswith("+00:00")


def test_multi_turn_chat_over_http(api):
    sid = new_session(api)
    a = send(api, sid, "How much does the Shopify Gold plan cost?").json()
    assert a["status"] == "answered" and "5,999" in a["message"]["content"]
    assert a["sources"][0]["url"] == "https://webstore.vgroup.net/shopify-pricing/"
    assert a["message"]["sources"][0]["number"] == 1
    assert a["recommendations"]["primary"]["name"] == "Shopify Packages"
    assert a["notification"]["sound"] == "message_received"
    b = send(api, sid, "And how long does it take to deliver?").json()
    assert "25 business days" in b["message"]["content"]
    assert b["debug"]["search_query"].startswith("Shopify Packages")
    state = api.get(f"/sessions/{sid}").json()
    assert state["message_count"] == 5


def test_irrelevant_limit_over_http(api):
    sid = new_session(api)
    r1 = send(api, sid, "Tell me a joke").json()
    assert r1["status"] == "off_topic" and r1["session"]["attempts_remaining"] == 2
    send(api, sid, "What's the capital of Australia?")
    r3 = send(api, sid, "Recommend a good movie").json()
    assert r3["status"] == "session_end" and r3["session"]["status"] == "closed"
    assert r3["session"]["close_reason"] == "irrelevant_limit" and r3["notification"]["sound"] == "session_ended"
    r4 = send(api, sid, "What is SMTU?")
    assert r4.status_code == 409 and r4.json()["detail"] == "This chat has ended. Please start a new chat."


def test_contact_flow_over_http(api):
    sid = new_session(api)
    r = send(api, sid, "I'd like to speak to your support team").json()
    assert r["action"]["type"] == "contact_form" and r["action"]["submit_endpoint"] == "/contact"
    assert r["session"]["flow"] == "awaiting_contact_form"
    bad = api.post(f"/sessions/{sid}/contact", json={"name": "A", "email": "bad", "message": "hi"})
    assert bad.status_code == 422 and {e["field"] for e in bad.json()["errors"]} >= {"name", "email", "message"}
    ok = api.post(f"/sessions/{sid}/contact", json={"name": "Rahul Verma", "email": "rahul@example.com",
                                                     "message": "Our Magento store needs ongoing support"})
    body = ok.json()
    assert ok.status_code == 201 and body["lead_id"]
    assert body["message"]["content"].startswith("Thank you, Rahul!")
    assert body["session"]["flow"] == "awaiting_anything_else"
    end = send(api, sid, "no, that's all").json()
    assert end["status"] == "session_end" and end["action"]["type"] == "feedback"
    sent = api.services.mailbox.sent
    assert [e.email_type for e in sent] == ["summary"] and sent[0].to == "rahul@example.com"


def test_polling_returns_idle_check_and_close(api):
    sid = new_session(api, email="idle@example.com")
    send(api, sid, "What is SMTU?")
    last_seq = api.get(f"/sessions/{sid}/messages").json()["messages"][-1]["seq"]
    api.clock.advance(api.services.settings.idle_check_after_seconds)
    poll = api.get(f"/sessions/{sid}/messages", params={"after": last_seq}).json()
    assert [m["kind"] for m in poll["messages"]] == ["idle_check"]
    assert poll["session"]["status"] == "awaiting_idle_response" and poll["notification"]["sound"] == "idle_check"
    api.clock.advance(api.services.settings.idle_close_after_seconds)
    poll2 = api.get(f"/sessions/{sid}/messages", params={"after": poll["messages"][-1]["seq"]}).json()
    assert poll2["messages"][0]["kind"] == "session_end" and poll2["session"]["close_reason"] == "idle_timeout"
    assert api.services.mailbox.sent[-1].email_type == "followup"


def test_sound_preference(api):
    sid = new_session(api)
    r = api.patch(f"/sessions/{sid}/preferences", json={"sound_enabled": False})
    assert r.status_code == 200 and r.json()["sound_enabled"] is False
    assert send(api, sid, "What is SMTU?").json()["notification"]["sound"] is None
    api.patch(f"/sessions/{sid}/preferences", json={"sound_enabled": True})
    assert send(api, sid, "What is ReviewCaddy?").json()["notification"]["sound"] == "message_received"


def test_transcript_download(api):
    sid = new_session(api)
    send(api, sid, "What is SMTU?")
    r = api.get(f"/sessions/{sid}/transcript")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert re.search(r'attachment; filename="vgroup-chat-transcript-\d{8}-\d{4}-[0-9a-f]{8}\.txt"',
                     r.headers["content-disposition"])
    assert "V Group - Chat Transcript" in r.text and "What is SMTU?" in r.text


def test_email_endpoint(api):
    sid = new_session(api)
    send(api, sid, "What is SMTU?")
    assert api.post(f"/sessions/{sid}/emails", json={"type": "transcript"}).status_code == 422  # no address
    r = api.post(f"/sessions/{sid}/emails", json={"type": "transcript", "email": "me@example.com"})
    assert r.status_code == 202 and r.json()["queued"] is True and "inbox" in r.json()["message"]
    assert api.post(f"/sessions/{sid}/emails", json={"type": "spam"}).status_code == 422
    assert api.post(f"/sessions/{sid}/emails", json={"type": "summary", "email": "nope"}).status_code == 422


def test_feedback_endpoints(api):
    sid = new_session(api)
    api.post(f"/sessions/{sid}/close")
    r = api.post(f"/sessions/{sid}/feedback", json={"rating": "Great", "comment": "Quick and clear"})
    assert r.status_code == 200 and r.json()["message"]["kind"] == "feedback_ack"
    assert api.post(f"/sessions/{sid}/feedback", json={"rating": "Amazing"}).status_code == 422
    token = api.services.feedback.token_for(sid)
    page = api.get(f"/feedback/{token}", params={"rating": "Poor"})
    assert page.status_code == 200 and "Thank you" in page.text
    assert api.services.feedback.get(sid)["rating"] == "Poor" and api.services.feedback.get(sid)["channel"] == "email"
    assert api.get("/feedback/forged-token", params={"rating": "Great"}).status_code == 400
    assert api.get(f"/feedback/{token}", params={"rating": "Meh"}).status_code == 400


def test_close_endpoint(api):
    sid = new_session(api, email="close@example.com")
    send(api, sid, "What is SMTU?")
    r = api.post(f"/sessions/{sid}/close").json()
    assert r["status"] == "session_end" and r["session"]["close_reason"] == "user_closed"
    assert api.post(f"/sessions/{sid}/close").status_code == 200  # idempotent


@pytest.mark.parametrize("method,path,body", [
    ("get", "/sessions/unknown", None), ("post", "/sessions/unknown/messages", {"message": "hi"}),
    ("get", "/sessions/unknown/transcript", None), ("post", "/sessions/unknown/feedback", {"rating": "OK"}),
    ("post", "/sessions/unknown/contact", {"name": "Jo Bloggs", "email": "jo@example.com", "message": "hello there"}),
])
def test_unknown_session_404(api, method, path, body):
    r = getattr(api, method)(path, json=body) if body else getattr(api, method)(path)
    assert r.status_code == 404 and "not found" in r.json()["detail"].lower()


@pytest.mark.parametrize("payload", [{}, {"message": ""}, {"message": "x" * 2001}, {"message": 5}])
def test_message_validation(api, payload):
    sid = new_session(api)
    r = api.post(f"/sessions/{sid}/messages", json=payload)
    assert r.status_code == 422 and "Traceback" not in r.text


def test_message_over_configured_limit(api):
    sid = new_session(api)
    r = api.post(f"/sessions/{sid}/messages", json={"message": "word " * 300})
    assert r.status_code == 422


def test_invalid_session_create(api):
    assert api.post("/sessions", json={"email": "not-an-email"}).status_code == 422


def test_production_hides_chat_debug(make_services, clock):
    services = make_services(clock=clock, idle_monitor_enabled=False, environment="production")
    with make_client(services) as c:
        sid = c.post("/sessions").json()["session"]["session_id"]
        body = c.post(f"/sessions/{sid}/messages", json={"message": "What is SMTU?"}).json()
    assert body["debug"] is None and "sentiment" not in str(body)


def test_ask_endpoint_still_works(api):
    r = api.post("/ask", json={"question": "How many support hours are included annually?"})
    assert r.status_code == 200 and "120" in r.json()["answer"]
