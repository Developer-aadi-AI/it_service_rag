"""Analytics events (no PII) and the summary report."""
from __future__ import annotations

import json
from datetime import timedelta

from app.analytics.events import normalize_question
from app.analytics.summary import format_text, summarize
from app.leads.service import LeadInput


def _conversation_mix(svc, clock):
    m = svc.manager
    a = m.start_session(email="pat@example.com").session.session_id
    for q in ["How much does the Shopify Gold plan cost?", "I want a blockchain-based NFT marketplace",
              "Tell me a joke", "I want to talk to your sales team"]:
        clock.advance(10)
        m.handle_message(a, q)
    m.submit_contact(a, LeadInput(name="Pat Lee", email="pat@example.com", phone="+1 609 555 0100",
                                  message="Call me about Shopify Gold"))
    clock.advance(10)
    m.handle_message(a, "no thanks")
    m.record_feedback(a, "Great")

    b = m.start_session().session.session_id
    for q in ["What is the weather in Paris today?", "Tell me a joke", "Who won the world cup?"]:
        clock.advance(5)
        m.handle_message(b, q)
    m.record_feedback(b, "Poor")

    c = m.start_session().session.session_id
    clock.advance(5)
    m.handle_message(c, "How much does the Shopify Gold plan cost?")
    clock.advance(svc.settings.idle_check_after_seconds)
    m.check_idle(m.get(c))
    clock.advance(svc.settings.idle_close_after_seconds)
    m.check_idle(m.get(c))
    return a, b, c


def test_summary_metrics(make_services, clock):
    svc = make_services(clock=clock)
    _conversation_mix(svc, clock)
    s = summarize(svc.db, clock() - timedelta(days=1), clock() + timedelta(seconds=1))
    sess, q = s["sessions"], s["questions"]
    assert sess["started"] == 3 and sess["closed"] == 3
    assert sess["close_reasons"] == {"user_ended": 1, "irrelevant_limit": 1, "idle_timeout": 1}
    assert sess["sessions_with_irrelevant_input"] == 2 and sess["avg_duration_s"] > 0
    assert q["by_status"]["answered"] == 2 and q["by_status"]["needs_team"] == 1
    assert q["no_answer_rate_pct"] == round(100 / 3, 1)
    assert q["top_questions"][0] == {"question": "how much does the shopify gold plan cost", "count": 2}
    assert s["recommendations"]["top_primary"][0]["service"] == "Shopify Packages"
    assert s["leads"]["submitted"] == 1 and s["leads"]["by_interest"][0]["service"] == "Shopify Packages"
    assert s["feedback"]["ratings"] == {"Great": 1, "OK": 0, "Poor": 1} and s["feedback"]["satisfaction_pct"] == 50.0
    assert s["emails"]["by_type"].get("summary") == 1 and s["errors"]["total"] == 0
    text = format_text(s)
    assert "Sessions" in text and "top questions" in text and "satisfaction 50.0%" in text


def test_events_contain_no_contact_details(make_services, clock):
    svc = make_services(clock=clock)
    _conversation_mix(svc, clock)
    dump = json.dumps(svc.db.query("SELECT * FROM events"))
    for pii in ("pat@example.com", "Pat Lee", "555 0100", "Call me about"):
        assert pii not in dump


def test_question_normalisation_masks_pii():
    assert normalize_question("My email is a.b@x.com, call +44 20 7946 0958??") == "my email is a***@x.com, call [phone]"


def test_analytics_can_be_disabled(make_services, clock):
    svc = make_services(clock=clock, analytics_enabled=False)
    sid = svc.manager.start_session().session.session_id
    svc.manager.handle_message(sid, "What is SMTU?")
    assert svc.db.query("SELECT COUNT(*) AS n FROM events")[0]["n"] == 0


def test_questions_not_stored_when_configured(make_services, clock):
    svc = make_services(clock=clock, analytics_store_questions=False)
    sid = svc.manager.start_session().session.session_id
    svc.manager.handle_message(sid, "What is SMTU?")
    row = svc.db.query("SELECT data FROM events WHERE event_type='message'")[0]
    assert json.loads(row["data"])["question"] is None


def test_error_events_for_email_failures(make_services, clock):
    svc = make_services(clock=clock)

    class Broken:
        name = "broken"

        def send(self, _):
            raise TimeoutError("smtp timeout")

    svc.emails.sender = Broken()
    sid = svc.manager.start_session(email="x@example.com").session.session_id
    svc.manager.send_email(sid, "transcript").result()
    s = summarize(svc.db, clock() - timedelta(days=1), clock() + timedelta(seconds=1))
    assert s["emails"]["failed"] == 1 and s["errors"]["by_component"] == {"email": 1}


def test_cli_text_report(make_services, clock, capsys, monkeypatch):
    import sys

    from app.analytics.__main__ import main

    svc = make_services(clock=clock)
    sid = svc.manager.start_session().session.session_id
    svc.manager.handle_message(sid, "What is SMTU?")
    monkeypatch.setattr(sys, "argv", ["analytics", "--db", str(svc.settings.db_path), "--days", "36500"])
    main()
    out = capsys.readouterr().out
    assert "D Group assistant - analytics summary" in out


def test_admin_analytics_endpoint(make_services, clock):
    from tests.conftest import make_client

    svc = make_services(clock=clock, admin_api_key="k" * 32, idle_monitor_enabled=False)
    with make_client(svc) as c:
        sid = c.post("/sessions").json()["session"]["session_id"]
        c.post(f"/sessions/{sid}/messages", json={"message": "What is SMTU?"})
        c.post("/ask", json={"question": "What is ReviewCaddy?"})
        # events are stamped with the fake clock (2026-10-08); look back far enough
        body = c.get("/admin/analytics", params={"days": 366}, headers={"X-Admin-Key": "k" * 32}).json()
        text = c.get("/admin/analytics.txt", params={"days": 366}, headers={"X-Admin-Key": "k" * 32}).text
    assert body["questions"]["total"] >= 2 and body["sessions"]["started"] == 1
    assert "analytics summary" in text
