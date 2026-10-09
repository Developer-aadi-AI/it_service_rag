"""Logging: correlation ids, redaction of secrets and PII, structured output."""
from __future__ import annotations

import json
import logging

from app.logging_config import (ContextFilter, JsonFormatter, redact, register_secrets, request_id_var,
                                scrub_pii, scrub_secrets, session_id_var)
from tests.conftest import make_client


def _record(msg, *args):
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)
    ContextFilter().filter(rec)
    return rec


def test_secrets_are_scrubbed():
    register_secrets("my-smtp-password-123")
    # fake key-shaped values, built at runtime so secret scanners don't flag the repository
    fake_anthropic = "sk-" + "ant-" + "api03-" + "A" * 20
    fake_groq = "gsk" + "_" + "B" * 20
    rec = _record("key=%s groq=%s smtp=%s password=%s", fake_anthropic, fake_groq,
                  "my-smtp-password-123", "hunter22")
    msg = rec.getMessage()
    for leaked in ("sk-ant-api03", "gsk_BBBB", "my-smtp-password-123", "hunter22"):
        assert leaked not in msg
    assert scrub_secrets("Authorization: Bearer abcdefghijklmnopqrstu") == "Authorization: Bearer ***"


def test_pii_is_masked_but_business_numbers_kept():
    text = scrub_pii("Email jane.doe@example.com or call +91 98765 43210 about the $3,999 plan from 2026")
    assert "jane.doe" not in text and "j***@example.com" in text
    assert "98765" not in text and "[phone]" in text
    assert "$3,999" in text and "2026" in text
    assert redact("x" * 200).endswith("…") and len(redact("x" * 200)) == 80


def test_context_ids_and_json_format():
    t1, t2 = request_id_var.set("req-123456"), session_id_var.set("abcd1234")
    try:
        rec = _record("hello %s", "world")
        payload = json.loads(JsonFormatter().format(rec))
    finally:
        request_id_var.reset(t1)
        session_id_var.reset(t2)
    assert payload["request_id"] == "req-123456" and payload["session_id"] == "abcd1234"
    assert payload["message"] == "hello world" and payload["level"] == "INFO" and payload["ts"].endswith("+00:00")


def test_request_id_header_and_access_log(make_services, caplog):
    svc = make_services(idle_monitor_enabled=False)
    with make_client(svc) as c, caplog.at_level(logging.INFO):
        r = c.get("/health", headers={"X-Request-ID": "demo-request-0001"})
        sid = c.post("/sessions").json()["session"]["session_id"]
        c.post(f"/sessions/{sid}/messages", json={"message": "My email is jane@example.com - what is SMTU?"})
        bad = c.get("/health", headers={"X-Request-ID": "bad id with spaces <script>"})
    assert r.headers["X-Request-ID"] == "demo-request-0001"
    assert bad.headers["X-Request-ID"] != "bad id with spaces <script>"  # untrusted ids are replaced
    access = [rec.getMessage() for rec in caplog.records if rec.name == "app.access"]
    assert any("GET /health -> 200" in m for m in access)
    turns = [rec for rec in caplog.records if rec.getMessage().startswith("turn session=")]
    assert turns and turns[-1].session_id == sid[:8]
    assert all("jane@example.com" not in rec.getMessage() for rec in caplog.records)
    assert "sources=" in turns[-1].getMessage() and "ms q=" in turns[-1].getMessage()
