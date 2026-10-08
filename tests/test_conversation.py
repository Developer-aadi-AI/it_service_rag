"""Multi-turn conversations: memory, follow-ups, flows, irrelevant limit, idle, sentiment, contact."""
from __future__ import annotations

import pytest

from app.conversation.models import Session
from app.conversation.store import SessionClosed, SessionNotFound
from app.leads.service import LeadInput
from app.rag.prompts import OFF_TOPIC_REPLY, PROJECT_IDEA_REPLY


@pytest.fixture
def svc(make_services, clock):
    return make_services(clock=clock)


def start(svc, **kw) -> str:
    return svc.manager.start_session(**kw).session.session_id


def say(svc, sid, text, clock=None):
    if clock:
        clock.advance(5)
    return svc.manager.handle_message(sid, text)


# --- memory ------------------------------------------------------------------------

def test_session_starts_with_greeting(svc):
    r = svc.manager.start_session()
    assert r.reply.kind == "greeting" and "V Group" in r.reply.content
    assert r.session.status == "active"


def test_memory_records_turns_with_utc_timestamps(svc, clock):
    sid = start(svc)
    say(svc, sid, "What is SMTU?", clock)
    say(svc, sid, "What is ReviewCaddy?", clock)
    s = svc.manager.get(sid)
    roles = [m.role for m in s.messages]
    assert roles == ["assistant", "user", "assistant", "user", "assistant"]
    assert [m.seq for m in s.messages] == [1, 2, 3, 4, 5]
    assert all(m.created_at.tzinfo is not None and m.created_at.utcoffset().total_seconds() == 0 for m in s.messages)


def test_history_is_controlled(svc):
    s = Session.new(svc.manager.clock())
    for i in range(10):
        s.add("user", f"question {i}", svc.manager.clock(), 200)
        s.add("assistant", f"answer {i}", svc.manager.clock(), 200)
    h = s.llm_history(3)
    assert len(h) == 6 and h[0] == {"role": "user", "content": "question 7"}
    assert s.llm_history(0) == []
    small = Session.new(svc.manager.clock())
    for i in range(30):
        small.add("user", str(i), svc.manager.clock(), max_messages=10)
    assert len(small.messages) == 10 and small.messages[-1].content == "29"


def test_history_passed_to_pipeline(svc, clock, monkeypatch):
    calls = []
    original = svc.manager.pipeline.answer

    def spy(question, history=None, *a, **kw):
        calls.append(history)
        return original(question, history, *a, **kw)

    monkeypatch.setattr(svc.manager.pipeline, "answer", spy)
    sid = start(svc)
    say(svc, sid, "What is SMTU?", clock)
    say(svc, sid, "What is ReviewCaddy?", clock)
    assert calls[0] == [] or calls[0] is None or all(h["role"] != "user" for h in calls[0])
    assert calls[1][0] == {"role": "user", "content": "What is SMTU?"}
    assert calls[1][1]["role"] == "assistant" and "SMTU" in calls[1][1]["content"]


# --- follow-ups ------------------------------------------------------------------

def test_pronoun_follow_up_uses_previous_topic(svc, clock):
    sid = start(svc)
    first = say(svc, sid, "How much does the Shopify Gold plan cost?", clock)
    assert "5,999" in first.reply.content
    r = say(svc, sid, "And how long does it take to deliver?", clock)
    assert r.status == "answered"
    assert "Shopify Packages" in r.rag.search_query
    assert "25 business days" in r.reply.content and r.rag.sources[0].page_id == "db-1859"


def test_follow_up_about_a_product(svc, clock):
    sid = start(svc)
    say(svc, sid, "Tell me about ReviewCaddy", clock)
    r = say(svc, sid, "Does it work with Shopify?", clock)
    assert r.status == "answered" and "ReviewCaddy" in r.rag.search_query
    assert "Shopify" in r.reply.content and "Review" in r.reply.content


def test_elliptical_follow_up_substitutes_subject(svc, clock):
    sid = start(svc)
    say(svc, sid, "How much does the Shopify Gold plan cost?", clock)
    r = say(svc, sid, "What about BigCommerce?", clock)
    assert r.rag.search_query == "How much does the BigCommerce Gold plan cost?"
    assert "BigCommerce Gold Plan is priced at $5,999" in r.reply.content


def test_off_topic_follow_up_is_not_rewritten(svc, clock):
    sid = start(svc)
    say(svc, sid, "How much does the Shopify Gold plan cost?", clock)
    r = say(svc, sid, "What about the weather in Paris?", clock)
    assert r.status == "off_topic"


def test_standalone_question_is_not_rewritten(svc, clock):
    sid = start(svc)
    say(svc, sid, "How much does the Shopify Gold plan cost?", clock)
    r = say(svc, sid, "Do you build mobile apps for iOS and Android?", clock)
    assert r.rag.search_query == "Do you build mobile apps for iOS and Android?"


def test_llm_rewrite_used_when_available(svc, clock, fake_llm_factory):
    llm = fake_llm_factory(['{"question": "How long does the Shopify Gold package take?"}'])
    svc.manager.rewriter.llm = llm
    sid = start(svc)
    say(svc, sid, "How much does the Shopify Gold plan cost?", clock)
    r = say(svc, sid, "how long does that take?", clock)
    assert r.rag.search_query == "How long does the Shopify Gold package take?"
    assert "Latest message: how long does that take?" in llm.calls[0]["messages"][0]["content"]


# --- recommendations in chat -----------------------------------------------------------

def test_recommendations_in_chat(svc, clock):
    sid = start(svc)
    r = say(svc, sid, "I need a mobile app for my business, what do you recommend?", clock)
    assert r.recommendations["primary"]["name"] == "Mobile App Development Services"
    assert len(r.recommendations["related"]) == 2
    assert "Based on what you've described, our Mobile App Development Services" in r.reply.content
    plain = say(svc, sid, "What is SMTU?", clock)
    assert plain.recommendations["primary"]["name"] == "SMTU"
    assert "Based on what you've described" not in plain.reply.content  # only when asked / project request


def test_no_recommendations_for_off_topic(svc, clock):
    r = say(svc, start(svc), "Tell me a joke", clock)
    assert r.recommendations is None


# --- sentiment -------------------------------------------------------------------------

def test_negative_user_gets_acknowledgement_then_help(svc, clock):
    r = say(svc, start(svc), "I'm having a problem - how many hours does Annual Support include?", clock)
    assert r.sentiment.label == "negative"
    first_sentence = r.reply.content.split(".")[0]
    assert "sorry" in first_sentence.lower()
    assert "120 hours" in r.reply.content  # still answers


def test_neutral_user_gets_no_acknowledgement(svc, clock):
    r = say(svc, start(svc), "How many hours does Annual Support include?", clock)
    assert "sorry" not in r.reply.content.lower() and "frustrat" not in r.reply.content.lower()


def test_acknowledgement_not_repeated_every_turn(svc, clock):
    sid = start(svc)
    a = say(svc, sid, "My store is broken. What is included in Annual Support?", clock)
    b = say(svc, sid, "Still broken. How many support hours are included annually?", clock)
    assert "sorry" in a.reply.content.lower()
    assert not b.reply.content.lower().startswith(("sorry", "i'm sorry"))


def test_repeated_frustration_offers_the_team(svc, clock):
    sid = start(svc)
    say(svc, sid, "This is frustrating. What does Annual Support cost?", clock)
    r = say(svc, sid, "Ugh, this is ridiculous. How many hours do I get?", clock)
    assert r.action and r.action["type"] == "contact_form" and r.action["reason"] == "frustration"
    assert "support team will connect with you shortly" in r.reply.content


def test_frustrated_complaint_about_current_topic_is_not_a_strike(svc, clock):
    sid = start(svc)
    say(svc, sid, "What is included in Annual Support?", clock)
    r = say(svc, sid, "This is useless, you are not helping at all!!", clock)
    assert r.status == "needs_team" and r.action["type"] == "contact_form"
    assert svc.manager.get(sid).irrelevant_count == 0
    assert "great question" not in r.reply.content.lower()


# --- irrelevant-input limit -----------------------------------------------------------------

def test_three_irrelevant_inputs_end_the_session(svc, clock):
    sid = start(svc)
    r1 = say(svc, sid, "What is the weather in Paris today?", clock)
    assert r1.status == "off_topic" and OFF_TOPIC_REPLY in r1.reply.content
    assert "You have 2 more attempts" in r1.reply.content and r1.attempts_remaining == 2
    r2 = say(svc, sid, "Tell me a joke", clock)
    assert "You have 1 more attempt before" in r2.reply.content and r2.attempts_remaining == 1
    r3 = say(svc, sid, "Who won the 2022 world cup?", clock)
    assert r3.status == "session_end" and r3.session.close_reason == "irrelevant_limit"
    assert "closing this chat" in r3.reply.content and r3.attempts_remaining == 0
    with pytest.raises(SessionClosed):
        say(svc, sid, "What is SMTU?")


def test_irrelevant_limit_is_configurable(make_services, clock):
    svc = make_services(clock=clock, irrelevant_limit=1)
    r = say(svc, start(svc), "Tell me a joke", clock)
    assert r.status == "session_end"


def test_relevant_questions_do_not_count(svc, clock):
    sid = start(svc)
    say(svc, sid, "Tell me a joke", clock)
    say(svc, sid, "What is SMTU?", clock)
    say(svc, sid, "hi", clock)  # small talk is not irrelevant
    assert svc.manager.get(sid).irrelevant_count == 1


def test_reset_on_relevant_option(make_services, clock):
    svc = make_services(clock=clock, irrelevant_reset_on_relevant=True)
    sid = start(svc)
    say(svc, sid, "Tell me a joke", clock)
    say(svc, sid, "Tell me a joke", clock)
    say(svc, sid, "What is SMTU?", clock)
    assert svc.manager.get(sid).irrelevant_count == 0


def test_no_answer_in_domain_is_not_irrelevant(svc, clock):
    sid = start(svc)
    r = say(svc, sid, "I want a blockchain-based NFT marketplace", clock)
    assert r.status == "needs_team" and r.reply.content == PROJECT_IDEA_REPLY
    assert svc.manager.get(sid).irrelevant_count == 0


# --- contact / lead flow ------------------------------------------------------------------------

def test_contact_request_opens_form(svc, clock):
    sid = start(svc)
    r = say(svc, sid, "I want to talk to your sales team", clock)
    assert r.status == "contact_prompt" and r.action["type"] == "contact_form"
    assert svc.manager.get(sid).flow == "awaiting_contact_form"


def test_contact_submission_then_no_closes_politely(svc, clock):
    sid = start(svc)
    say(svc, sid, "How much does the Shopify Gold plan cost?", clock)
    say(svc, sid, "Can someone call me back?", clock)
    r = svc.manager.submit_contact(sid, LeadInput(name="Priya Sharma", email="priya@example.com",
                                                  phone="+91 98765 43210", message="Interested in the Gold plan"))
    assert r.status == "lead_confirmation"
    assert r.reply.content.startswith("Thank you, Priya!")
    assert "connect with you shortly" in r.reply.content and r.reply.content.endswith("anything else I can help you with?")
    assert r.lead.interest == "Shopify Packages" and r.lead.session_id == sid
    assert svc.leads.get(r.lead.lead_id).phone == "+91 98765 43210"
    end = say(svc, sid, "No thanks", clock)
    assert end.status == "session_end" and end.session.close_reason == "user_ended"
    assert end.action["type"] == "feedback" and end.action["options"] == ["Great", "OK", "Poor"]
    assert "summary of our conversation is on its way" in end.reply.content
    assert [e.email_type for e in svc.mailbox.sent] == ["summary"]


def test_contact_submission_then_yes_continues(svc, clock):
    sid = start(svc)
    say(svc, sid, "Please contact me", clock)
    svc.manager.submit_contact(sid, LeadInput(name="Alex Lee", email="alex@example.com", message="Need a quote please"))
    r = say(svc, sid, "yes", clock)
    assert r.status == "continue" and "what else" in r.reply.content.lower()
    r = say(svc, sid, "What is SMTU?", clock)
    assert r.status == "answered" and svc.manager.get(sid).status == "active"


def test_contact_submission_then_question_answers_it(svc, clock):
    sid = start(svc)
    say(svc, sid, "Please contact me", clock)
    svc.manager.submit_contact(sid, LeadInput(name="Alex Lee", email="alex@example.com", message="Need a quote please"))
    r = say(svc, sid, "How many hours does Annual Support include?", clock)
    assert r.status == "answered" and "120" in r.reply.content


def test_team_notified_of_lead(make_services, clock):
    svc = make_services(clock=clock, team_notification_email="sales-team@example.com")
    sid = start(svc)
    say(svc, sid, "What is ReviewCaddy?", clock)
    svc.manager.submit_contact(sid, LeadInput(name="Sam Patel", email="sam@example.com", message="Pricing for ReviewCaddy"))
    team = [e for e in svc.mailbox.sent if e.email_type == "team_lead"]
    assert len(team) == 1 and team[0].to == "sales-team@example.com" and team[0].reply_to == "sam@example.com"
    assert "Sam Patel" in team[0].text and "ReviewCaddy" in team[0].subject


# --- goodbye / closing -------------------------------------------------------------------------

@pytest.mark.parametrize("bye", ["bye", "Goodbye!", "that's all"])
def test_goodbye_closes_gracefully(svc, clock, bye):
    sid = start(svc)
    say(svc, sid, "What is SMTU?", clock)
    r = say(svc, sid, bye, clock)
    assert r.status == "session_end" and r.action["type"] == "feedback"
    assert "Thank you for chatting with V Group" in r.reply.content
    assert "inbox" not in r.reply.content  # no email address known -> no email promised


def test_no_after_anything_else_closing_line(svc, clock):
    sid = start(svc)
    r = say(svc, sid, "What is SMTU?", clock)
    if "anything else" in r.reply.content.lower():
        assert say(svc, sid, "no", clock).status == "session_end"


def test_user_close_is_idempotent(svc):
    sid = start(svc)
    a = svc.manager.close_by_user(sid)
    b = svc.manager.close_by_user(sid)
    assert a.session.close_reason == "user_closed" and b.status == "session_end"
    assert len([m for m in svc.manager.get(sid).messages if m.kind == "session_end"]) == 1


def test_transcript_saved_on_close(svc, clock):
    sid = start(svc)
    say(svc, sid, "What is SMTU?", clock)
    say(svc, sid, "bye", clock)
    files = list(svc.settings.transcripts_path.glob("*.txt"))
    assert len(files) == 1 and "What is SMTU?" in files[0].read_text(encoding="utf-8")


# --- idle sessions -------------------------------------------------------------------------------

def test_idle_check_then_close_with_followup_email(svc, clock):
    sid = start(svc, email="idle.customer@example.com")
    say(svc, sid, "What is SMTU?", clock)
    s = svc.manager.get(sid)
    clock.advance(svc.settings.idle_check_after_seconds - 1)
    assert svc.manager.check_idle(s) == []
    clock.advance(1)
    check = svc.manager.check_idle(s)
    assert len(check) == 1 and check[0].kind == "idle_check" and "Are you still there?" in check[0].content
    assert s.status == "awaiting_idle_response"
    assert svc.manager.check_idle(s) == []  # not repeated
    clock.advance(svc.settings.idle_close_after_seconds)
    end = svc.manager.check_idle(s)
    assert end[0].kind == "session_end" and "didn't hear back from you" in end[0].content
    assert "on its way to your inbox" in end[0].content
    assert s.close_reason == "idle_timeout"
    followups = [e for e in svc.mailbox.sent if e.email_type == "followup"]
    assert len(followups) == 1 and followups[0].to == "idle.customer@example.com"
    assert followups[0].attachments and followups[0].attachments[0].filename.endswith(".txt")


def test_reply_after_idle_check_keeps_session(svc, clock):
    sid = start(svc)
    say(svc, sid, "What is SMTU?", clock)
    s = svc.manager.get(sid)
    clock.advance(svc.settings.idle_check_after_seconds)
    svc.manager.check_idle(s)
    r = say(svc, sid, "Yes, I'm here. What is ReviewCaddy?", clock)
    assert r.status == "answered" and s.status == "active"
    clock.advance(svc.settings.idle_close_after_seconds)
    assert svc.manager.check_idle(s) == []  # timer restarted


def test_idle_timings_configurable_and_no_email_without_address(make_services, clock):
    svc = make_services(clock=clock, idle_check_after_seconds=10, idle_close_after_seconds=5)
    sid = start(svc)
    say(svc, sid, "What is SMTU?", clock)
    clock.advance(10)
    msg = svc.manager.check_idle(svc.manager.get(sid))[0]
    assert "about 5 seconds" in msg.content
    clock.advance(5)
    end = svc.manager.check_idle(svc.manager.get(sid))[0]
    assert "inbox" not in end.content and svc.mailbox.sent == []


def test_untouched_session_is_not_idle_checked(svc, clock):
    sid = start(svc)
    clock.advance(10_000)
    assert svc.manager.check_idle(svc.manager.get(sid)) == []


def test_sweep_and_purge(make_services, clock):
    svc = make_services(clock=clock, session_retention_seconds=3600)
    sid = start(svc)
    say(svc, sid, "What is SMTU?", clock)
    clock.advance(svc.settings.idle_check_after_seconds)
    assert svc.manager.sweep() == 1
    clock.advance(4000)
    svc.manager.sweep()  # closes the idle session (closing counts as activity)
    clock.advance(4000)
    svc.manager.sweep()  # retention elapsed -> purged
    with pytest.raises(SessionNotFound):
        svc.manager.get(sid)


# --- errors ------------------------------------------------------------------------------------

def test_errors(svc):
    with pytest.raises(SessionNotFound):
        svc.manager.handle_message("does-not-exist", "hi")
    sid = start(svc)
    with pytest.raises(ValueError):
        svc.manager.handle_message(sid, "   ")
    with pytest.raises(ValueError):
        svc.manager.handle_message(sid, "x" * (svc.settings.max_message_chars + 1))
    with pytest.raises(ValueError):
        svc.manager.start_session(email="not-an-email")
