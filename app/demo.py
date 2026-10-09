"""Demonstration conversations (and a pre-demo smoke test).

    python -m app.demo                # run all 10 scenarios, print the conversations, check expectations
    python -m app.demo --only 3,7     # selected scenarios
    python -m app.demo --markdown docs/DEMO_RUN.md

Every question uses information that exists in the supplied D Group data. Runs with the
configured answer mode (LLM if a key is set, offline otherwise), a simulated clock for the
inactivity scenario, an in-memory mailbox and temporary storage (nothing is sent or kept).
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from app.leads.service import LeadInput


@dataclass
class Scenario:
    number: int
    title: str
    shows: str
    steps: list  # str (user message) or callable(manager, session_id) -> list[str] of extra lines
    expect: Callable[[dict], list[str]]  # returns failed expectations
    start: dict = field(default_factory=dict)


class DemoClock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _submit_lead(name, email, message):
    def step(ctx):
        r = ctx["manager"].submit_contact(ctx["sid"], LeadInput(name=name, email=email, message=message))
        ctx["lead"] = r.lead
        return [f"[contact form submitted: {name} <{email}>]", f"ASSISTANT: {r.reply.content}"]
    return step


def _idle(ctx):
    m, clock, s = ctx["manager"], ctx["clock"], ctx["manager"].get(ctx["sid"])
    lines = []
    clock.advance(m.settings.idle_check_after_seconds)
    lines += [f"[{m.settings.idle_check_after_seconds}s without a reply]"] + [f"ASSISTANT: {x.content}" for x in m.check_idle(s)]
    clock.advance(m.settings.idle_close_after_seconds)
    lines += [f"[{m.settings.idle_close_after_seconds}s more]"] + [f"ASSISTANT: {x.content}" for x in m.check_idle(s)]
    return lines


def _email_transcript(ctx):
    ctx["manager"].send_email(ctx["sid"], "transcript").result()
    sent = ctx["mailbox"].sent[-1]
    return [f"[email '{sent.subject}' sent to {sent.to} with {sent.attachments[0].filename}]"]


def _feedback(rating):
    def step(ctx):
        msg = ctx["manager"].record_feedback(ctx["sid"], rating)
        return [f"[customer clicks {rating}]", f"ASSISTANT: {msg.content}"]
    return step


def _has(text):
    return lambda ctx: [] if text.lower() in ctx["log"].lower() else [f"expected '{text}' in the conversation"]


def _all(*checks):
    return lambda ctx: [f for c in checks for f in c(ctx)]


def _status(index, *statuses):
    def check(ctx):
        got = ctx["statuses"][index]
        return [] if got in statuses else [f"turn {index + 1}: expected {statuses}, got {got}"]
    return check


SCENARIOS = [
    Scenario(1, "Service question", "grounded answer with sources",
             ["Do you build mobile apps for iOS and Android?"],
             _all(_status(0, "answered"), _has("iOS"), _has("Sources:"))),
    Scenario(2, "Follow-up question", "memory: 'it' resolves to the previous topic",
             ["How much does the Shopify Gold plan cost?", "And how long does it take to deliver?"],
             _all(_status(0, "answered"), _has("5,999"), _has("25 business days"))),
    Scenario(3, "Service recommendation", "primary + related services from the catalog",
             ["I need a mobile app for my business, what do you recommend?"],
             _all(_status(0, "answered"), _has("Mobile App Development"), _has("Recommended:"))),
    Scenario(4, "Frustrated user", "brief acknowledgement, help, then the team is offered",
             ["This is so frustrating - how many support hours does Annual Support include?",
              "Ugh, this is ridiculous. What does it cost?"],
             _all(_has("120"), _has("support team will connect with you shortly"))),
    Scenario(5, "No-answer question", "never 'I don't know': project idea -> team + form",
             ["I want a blockchain-based NFT marketplace"],
             _all(_status(0, "needs_team"), _has("That sounds like a great idea"), _has("[form: contact_form]"))),
    Scenario(6, "Irrelevant questions", "3-attempt rule with remaining attempts",
             ["What is the weather in Paris today?", "Tell me a joke", "Who won the 2022 world cup?"],
             _all(_has("2 more attempts"), _has("1 more attempt"), _status(2, "session_end"))),
    Scenario(7, "Lead / contact capture", "form -> confirmation -> 'anything else?' -> polite close",
             ["How much does the BigCommerce Silver plan cost?", "I want to talk to your sales team",
              _submit_lead("Asha Rao", "asha.rao@example.com", "We'd like a quote for a BigCommerce store"), "No thanks"],
             _all(_has("Thank you, Asha!"), _has("connect with you shortly"), _has("Thank you for chatting with D Group"))),
    Scenario(8, "Session timeout", "are-you-still-there check, then close + follow-up email",
             ["What is SMTU?", _idle], _all(_has("Are you still there?"), _has("didn't hear back from you"),
                                            _has("inbox")), start={"email": "demo.visitor@example.com"}),
    Scenario(9, "Transcript & email", "transcript emailed to the address the customer gave",
             ["What is included in Annual Support?", _email_transcript],
             _all(_has("Your D Group chat transcript"), _has(".txt")), start={"email": "demo.visitor@example.com"}),
    Scenario(10, "Feedback", "goodbye -> feedback prompt -> rating stored",
             ["What is ReviewCaddy?", "bye", _feedback("Great")],
             _all(_has("[form: feedback]"), _has("glad the chat was helpful"))),
]


def run(selected: set[int] | None = None, bundle=None, settings=None) -> tuple[list[str], int]:
    from app.config import get_settings
    from app.email.senders import MemoryEmailSender
    from app.logging_config import setup_logging
    from app.rag.pipeline import build_pipeline
    from app.services import build_services

    setup_logging("WARNING")
    settings = settings or get_settings()
    bundle = bundle or build_pipeline(settings)
    clock, mailbox = DemoClock(), MemoryEmailSender()
    demo_settings = settings.model_copy(update={"storage_dir": Path(tempfile.mkdtemp(prefix="dgroup-demo-")),
                                                "idle_monitor_enabled": False})
    svc = build_services(demo_settings, bundle, clock=clock, email_sender=mailbox, background_email=False)
    out, failed = [f"# D Group assistant - demo conversations ({bundle.pipeline.mode})", ""], 0
    try:
        for sc in SCENARIOS:
            if selected and sc.number not in selected:
                continue
            res = svc.manager.start_session(**sc.start)
            ctx = {"manager": svc.manager, "sid": res.session.session_id, "clock": clock, "mailbox": mailbox,
                   "statuses": []}
            lines = [f"## {sc.number}. {sc.title}", f"_Shows: {sc.shows}_", "", f"ASSISTANT: {res.reply.content}"]
            for step in sc.steps:
                clock.advance(8)
                if callable(step):
                    lines += step(ctx)
                    continue
                r = svc.manager.handle_message(ctx["sid"], step)
                ctx["statuses"].append(r.status)
                lines += [f"USER: {step}", f"ASSISTANT: {r.reply.content}"]
                if r.rag and r.rag.sources:
                    lines.append("  Sources: " + "; ".join(f"[{s.number}] {s.title} ({s.url})" for s in r.rag.sources))
                if r.recommendations:
                    rec = r.recommendations
                    lines.append(f"  Recommended: {rec['primary']['name']}"
                                 + (f" | related: {', '.join(x['name'] for x in rec['related'])}" if rec["related"] else ""))
                if r.action:
                    lines.append(f"  [form: {r.action['type']}]")
            ctx["log"] = "\n".join(lines)
            problems = sc.expect(ctx)
            failed += bool(problems)
            lines += ["", "**Check:** " + ("PASS" if not problems else "FAIL - " + "; ".join(problems)), ""]
            out += [re.sub(r"\n{3,}", "\n\n", line) for line in lines]
    finally:
        svc.shutdown()
    return out, failed


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the D Group demo conversations")
    parser.add_argument("--only", help="comma-separated scenario numbers")
    parser.add_argument("--markdown", help="also write the transcript to this markdown file")
    args = parser.parse_args()
    selected = {int(x) for x in args.only.split(",")} if args.only else None
    lines, failed = run(selected)
    text = "\n".join(lines)
    print(text)
    if args.markdown:
        Path(args.markdown).write_text(text + "\n", encoding="utf-8")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
