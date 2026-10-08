"""Lead validation/storage and feedback (chat + signed email links)."""
from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from app.feedback.service import InvalidFeedback, normalize_rating
from app.leads.service import LeadInput
from app.storage.database import mask_email


@pytest.fixture
def svc(make_services, clock):
    return make_services(clock=clock)


def test_lead_input_cleans_and_validates():
    lead = LeadInput(name="  Priya   Sharma ", email="priya@example.com", phone=" +91 98765 43210 ",
                     company=" Acme\x00 Ltd ", message="  I need   a quote  ")
    assert lead.name == "Priya Sharma" and lead.company == "Acme Ltd" and lead.message == "I need a quote"
    assert lead.phone == "+91 98765 43210"


@pytest.mark.parametrize("field,value", [
    ("name", "J"), ("name", "<script>alert(1)</script>"), ("name", "Robert'); DROP TABLE leads;--{"),
    ("email", "not-an-email"), ("email", "a@b"), ("phone", "call me maybe"), ("phone", "12-34"),
    ("message", "hi"), ("message", "x" * 2001),
])
def test_lead_input_rejects_bad_values(field, value):
    data = {"name": "Priya Sharma", "email": "priya@example.com", "message": "Interested in Shopify"}
    data[field] = value
    with pytest.raises(ValidationError):
        LeadInput(**data)


def test_lead_stored_with_parameterised_sql(svc):
    tricky = "O'Brien \"Quote\" Test"
    lead = svc.leads.create(LeadInput(name=tricky, email="ob@example.com", message="Robert'); DROP TABLE leads;--"),
                            session_id="s1", interest="Shopify Packages")
    stored = svc.leads.get(lead.lead_id)
    assert stored.name == tricky and stored.message.startswith("Robert');")
    assert svc.leads.for_session("s1")[0].lead_id == lead.lead_id
    assert stored.created_at.endswith("+00:00")


def test_lead_notification_failure_does_not_lose_lead(svc):
    def boom(_lead):
        raise RuntimeError("smtp down")

    svc.leads.on_created = boom
    lead = svc.leads.create(LeadInput(name="Sam Lee", email="sam@example.com", message="Need help with Magento"))
    assert svc.leads.get(lead.lead_id) is not None


def test_mask_email():
    assert mask_email("jane.doe@example.com") == "j***@example.com" and mask_email("bad") == "***"


# --- feedback ------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [("Great", "Great"), ("ok", "OK"), (" POOR ", "Poor")])
def test_normalize_rating(raw, expected):
    assert normalize_rating(raw) == expected


def test_invalid_rating():
    with pytest.raises(InvalidFeedback):
        normalize_rating("Excellent")


def test_feedback_in_chat_updates_once_per_session(svc):
    sid = svc.manager.start_session().session.session_id
    svc.manager.close_by_user(sid)
    msg = svc.manager.record_feedback(sid, "OK")
    assert msg.kind == "feedback_ack" and "Thank you for the feedback" in msg.content
    svc.manager.record_feedback(sid, "Great", comment="Very quick answers")
    row = svc.feedback.get(sid)
    assert row["rating"] == "Great" and row["comment"] == "Very quick answers" and row["channel"] == "chat"
    assert svc.manager.get(sid).feedback == "Great"


def test_poor_feedback_reply_is_apologetic_not_defensive(svc):
    sid = svc.manager.start_session().session.session_id
    msg = svc.manager.record_feedback(sid, "Poor")
    assert "sorry" in msg.content.lower() and "improve" in msg.content.lower()


def test_signed_feedback_tokens(svc):
    token = svc.feedback.token_for("abc123session")
    assert svc.feedback.session_from_token(token) == "abc123session"
    tampered = svc.feedback.token_for("other") [:-2] + "xx"
    with pytest.raises(InvalidFeedback):
        svc.feedback.session_from_token(tampered)
    with pytest.raises(InvalidFeedback):
        svc.feedback.session_from_token("%%%not-base64%%%")


def test_feedback_db_rejects_invalid_rating(svc):
    with pytest.raises(sqlite3.IntegrityError):
        svc.db.execute("INSERT INTO feedback (session_id, created_at, rating, channel) VALUES ('x', 'now', 'Meh', 'chat')")
