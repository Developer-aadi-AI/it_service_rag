"""Transcripts (.txt) and the email workflow (transcript, summary, follow-up, feedback)."""
from __future__ import annotations

import email
import re
from email import policy

import pytest

from app.email.senders import FileEmailSender, OutgoingEmail, SMTPEmailSender, Attachment, create_sender
from app.leads.service import LeadInput
from app.transcripts.builder import build_transcript, friendly_date, summarize, transcript_filename

INTERNAL = re.compile(r"top_score|similarity|chunk|page_id|generator|sentiment|frustrat|extractive|debug|"
                      r"request_kind|needs_team|off_topic|prompt|traceback|db-\d+", re.I)


@pytest.fixture
def svc(make_services, clock):
    return make_services(clock=clock)


def chat(svc, clock, *messages, **start):
    sid = svc.manager.start_session(**start).session.session_id
    for m in messages:
        clock.advance(7)
        svc.manager.handle_message(sid, m)
    return svc.manager.get(sid)


# --- transcript ------------------------------------------------------------------------------

def test_transcript_contents(svc, clock):
    s = chat(svc, clock, "What is SMTU?", "How many hours does Annual Support include?", name="Priya Sharma")
    svc.manager.close(s, "user_ended")
    text = build_transcript(s)
    assert text.startswith("D Group - Chat Transcript")
    assert "Date:       Thursday, 8 October 2026" in text
    assert "Started:    2026-10-08 09:30:00 UTC" in text and "Ended:" in text
    assert "Customer:   Priya Sharma" in text and "Status:     Conversation completed" in text
    assert "[09:30:07 UTC] You:" in text and "    What is SMTU?" in text
    assert "] D Group Assistant:" in text and "120" in text
    assert "Sources:" in text and "https://webstore.vgroup.net/" in text
    assert "All times are in Coordinated Universal Time (UTC)." in text
    assert not INTERNAL.search(text), INTERNAL.search(text)


def test_transcript_filename(svc, clock):
    s = chat(svc, clock, "What is SMTU?")
    name = transcript_filename(s)
    assert re.fullmatch(r"dgroup-chat-transcript-20261008-0930-[0-9a-f]{8}\.txt", name)


def test_friendly_date():
    from datetime import datetime, timezone

    assert friendly_date(datetime(2026, 1, 5, tzinfo=timezone.utc)) == "Monday, 5 January 2026"


def test_rule_based_summary(svc, clock):
    s = chat(svc, clock, "How much does the Shopify Gold plan cost?", "Tell me a joke")
    points = summarize(s)
    assert points[0] == "You asked about: How much does the Shopify Gold plan cost?"  # off-topic excluded
    assert any(p.startswith("Services we discussed: Shopify Packages") for p in points)


def test_llm_summary_used_when_available(svc, clock, fake_llm_factory):
    s = chat(svc, clock, "What is SMTU?")
    llm = fake_llm_factory(['{"points": ["You asked what SMTU is.", "We explained it syncs Shopify orders."]}'])
    assert summarize(s, llm) == ["You asked what SMTU is.", "We explained it syncs Shopify orders."]
    broken = fake_llm_factory(["not json"])
    assert summarize(s, broken)[0].startswith("You asked about:")  # falls back


# --- emails ----------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["transcript", "summary", "followup", "feedback"])
def test_each_email_type(svc, clock, kind):
    s = chat(svc, clock, "What is ReviewCaddy?", name="Alex Lee")
    svc.manager.send_email(s.session_id, kind, "alex@example.com").result()
    sent = svc.mailbox.sent[-1]
    assert sent.email_type == kind and sent.to == "alex@example.com"
    assert sent.text.startswith("Hi Alex,") and "The D Group team" in sent.text
    assert "webstore@vgroupinc.com" in sent.text  # footer from the supplied Contact Us page
    assert sent.html and "<html>" in sent.html
    for part in (sent.subject, sent.text, sent.html):
        assert not INTERNAL.search(part), (kind, INTERNAL.search(part))
    if kind in ("transcript", "summary", "followup"):
        att = sent.attachments[0]
        assert att.filename.endswith(".txt") and b"What is ReviewCaddy?" in att.content
    if kind in ("summary", "feedback"):
        links = re.findall(r"/feedback/([\w-]+)\?rating=(Great|OK|Poor)", sent.text)
        assert {r for _, r in links} == {"Great", "OK", "Poor"}
        assert svc.feedback.session_from_token(links[0][0]) == s.session_id


def test_user_content_is_escaped_in_html(svc, clock):
    s = chat(svc, clock, "What is SMTU?", name="<b>Eve</b>")
    svc.manager.send_email(s.session_id, "transcript", "eve@example.com").result()
    assert "<b>Eve</b>" not in svc.mailbox.sent[-1].html and "&lt;b&gt;Eve" in svc.mailbox.sent[-1].html


def test_email_requires_valid_address(svc, clock):
    s = chat(svc, clock, "What is SMTU?")
    with pytest.raises(ValueError):
        svc.manager.send_email(s.session_id, "transcript")  # no address known
    with pytest.raises(ValueError):
        svc.manager.send_email(s.session_id, "transcript", "nope")


def test_close_without_email_sends_nothing(svc, clock):
    s = chat(svc, clock, "What is SMTU?")
    svc.manager.close(s, "user_ended")
    assert svc.mailbox.sent == []


def test_email_on_close_can_be_disabled(make_services, clock):
    svc = make_services(clock=clock, email_on_close=False)
    s = chat(svc, clock, "What is SMTU?", email="a@example.com")
    end = svc.manager.close(s, "user_ended")
    assert svc.mailbox.sent == [] and "inbox" not in end.reply.content


def test_email_failure_is_logged_not_raised(svc, clock):
    class Broken:
        name = "broken"

        def send(self, _):
            raise ConnectionError("smtp unreachable")

    svc.emails.sender = Broken()
    s = chat(svc, clock, "What is SMTU?")
    assert svc.manager.send_email(s.session_id, "transcript", "x@example.com").result() is False
    log = svc.db.query("SELECT * FROM email_log ORDER BY id DESC LIMIT 1")[0]
    assert log["status"] == "failed" and log["error"] == "ConnectionError" and log["recipient"] == "x***@example.com"


def test_lead_followup_summary_mentions_next_step(svc, clock):
    s = chat(svc, clock, "How much does the Shopify Gold plan cost?")
    svc.manager.submit_contact(s.session_id, LeadInput(name="Jo Bloggs", email="jo@example.com", message="Gold plan quote"))
    svc.manager.send_email(s.session_id, "followup", None).result()
    sent = svc.mailbox.sent[-1]
    assert sent.to == "jo@example.com" and "We've received your request" in sent.subject
    assert "our team will connect with you shortly" in sent.text


# --- senders ----------------------------------------------------------------------------------

def test_file_sender_writes_valid_eml(tmp_path):
    sender = FileEmailSender(tmp_path, "D Group <no-reply@example.com>")
    sender.send(OutgoingEmail(to="a@example.com", subject="Hello", text="Plain", html="<p>Html</p>",
                              attachments=[Attachment("t.txt", b"transcript", "text/plain")], email_type="summary",
                              session_id="abcdef123"))
    files = list(tmp_path.glob("*.eml"))
    assert len(files) == 1 and "summary" in files[0].name
    msg = email.message_from_bytes(files[0].read_bytes(), policy=policy.default)
    assert msg["To"] == "a@example.com" and msg["Subject"] == "Hello"
    assert [p.get_filename() for p in msg.iter_attachments()] == ["t.txt"]


def test_smtp_sender_uses_starttls_and_login(monkeypatch):
    events = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            events.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            events.append(("starttls",))

        def login(self, user, pw):
            events.append(("login", user))

        def send_message(self, msg):
            events.append(("send", msg["To"]))

    monkeypatch.setattr("app.email.senders.smtplib.SMTP", FakeSMTP)
    SMTPEmailSender("smtp.example.com", 587, "D Group <a@example.com>", "user", "secret").send(
        OutgoingEmail(to="b@example.com", subject="s", text="t"))
    assert events == [("connect", "smtp.example.com", 587), ("starttls",), ("login", "user"), ("send", "b@example.com")]


def test_smtp_backend_requires_host(real_settings):
    with pytest.raises(ValueError):
        real_settings.model_validate({**real_settings.model_dump(), "email_backend": "smtp", "smtp_host": None})
    assert create_sender(real_settings.model_copy(update={"email_backend": "disabled"})).name == "disabled"
