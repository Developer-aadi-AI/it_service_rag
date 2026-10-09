# Security review (Phase 3)

Scope: the whole repository (API, conversation layer, RAG/LLM, email, storage, widget, configuration).
Each finding lists the risk, the fix and the test that covers it.

| # | Area | Finding | Severity | Fix | Test |
|---|---|---|---|---|---|
| 1 | Email | Any chat could email any address, unlimited times → open relay for spam (attacker types text, emails "transcript" to victims) | High | A chat can only email the address its customer gave (first address wins); max `EMAIL_MAX_PER_SESSION` emails; 429 beyond | `test_email_cannot_be_relayed_to_other_addresses`, `test_email_limit_per_session` |
| 2 | API abuse | No rate limiting; unbounded session creation could exhaust memory | High | Per-IP sliding-window limits (requests/min, new chats/hour), `MAX_ACTIVE_SESSIONS` cap with purge, 429/503 with `Retry-After` | `test_rate_limit_returns_429`, `test_session_creation_rate_limit`, `test_max_active_sessions` |
| 3 | Prompt injection | User text followed `QUESTION:` after the context, so a message could fake a `CONTEXT` block or "new rules" | Medium | Context and question wrapped in `<context>/<source>` and `<customer_question>` tags; tag-like text is stripped from user text and page text; system prompt states both are data; answers echoing the system prompt are discarded | `test_user_text_cannot_forge_context_blocks`, `test_context_text_cannot_close_its_block`, `test_answer_leaking_prompt_is_discarded`, evaluation hallucination probe |
| 4 | Grounding | LLM presented blog articles as D Group offers/prices (e.g. generic "$30–$300/month" website costs) | Medium | Prompt rule + code guard: offer/price questions answered only from blog posts are handed to the team | evaluation (no-answer, ambiguous) |
| 5 | Logging | User questions were logged verbatim (could contain emails/phones); no secret scrubbing | Medium | Log filter scrubs configured secret values, API-key patterns, `password=…`, bearer tokens; masks emails/phones in user text; analytics store only scrubbed, truncated questions | `test_secrets_are_scrubbed`, `test_pii_is_masked_but_business_numbers_kept`, `test_events_contain_no_contact_details` |
| 6 | Configuration | Production could start with `CORS_ORIGINS=*`, no `APP_SECRET_KEY` (feedback links unsigned across restarts), http public URL | Medium | `ENVIRONMENT=production` refuses unsafe settings at startup | `test_production_requires_safe_settings` |
| 7 | Configuration | Malformed LLM key in `.env` (placeholder value) made every request call the provider, fail and fall back | Low (availability) | Auto-selection skips keys that don't match the provider's format (logged without the value); circuit breaker pauses a failing provider | `test_auto_provider_skips_malformed_keys`, circuit-breaker tests |
| 8 | Transport/body | No request size limit | Low | `MAX_REQUEST_BYTES` (64 KB) → 413 | `test_request_body_size_limit` |
| 9 | Headers | No security headers | Low | `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, `X-Frame-Options: DENY` (API), strict CSP for the widget (no inline script/style) | `test_security_headers` |
| 10 | Docs exposure | Swagger/ReDoc always on | Low | Disabled in production | `test_production_hides_docs` |
| 11 | Error leakage | Already generic; added analytics error events without messages | — | Unhandled errors return a fixed message; details only in logs (with request id) | `test_unhandled_errors_do_not_leak` |
| 12 | Admin data | New analytics endpoint | — | Disabled unless `ADMIN_API_KEY`; constant-time key comparison | `test_admin_analytics_requires_key` |
| 13 | Widget XSS | — | — | All text rendered with `textContent`; links built with `href` + `rel="noopener noreferrer"`; CSP `script-src 'self'` | `test_widget_is_served_and_csp_safe` |

## Already in place (verified)

- Secrets only from environment / `.env`; `.env` is git-ignored and not tracked; `.env.example` has empty secret values (tested).
- Input validation on every endpoint (pydantic): lengths, email format, phone format, name characters, rating enum,
  control characters stripped from form fields.
- SQL: parameterised queries only (tested with injection-style input).
- Email: user content HTML-escaped in templates; header injection prevented (whitespace collapsed, `email` library).
- Feedback links: HMAC-signed tokens, constant-time comparison.
- Session ids: 128-bit random; transcripts/emails never include internal scores, prompts or ids beyond a short reference.
- CORS: no credentials; explicit methods and headers.

## Residual risks / recommendations

- Rate limits are per process and per IP; behind a proxy set `TRUST_PROXY_HEADERS=true` *only* if the proxy
  overwrites `X-Forwarded-For`. For multi-instance deployments enforce limits at the proxy or in Redis.
- Session ids are bearer tokens: always serve over HTTPS.
- The LLM can still paraphrase loosely; grounding is enforced by citations, the blog guard and evaluation, not by
  formal verification. Re-run `python -m app.evaluation --check` after prompt or model changes.
- Note for the repository owner: the local `.env` contains several real-looking API keys (it is not tracked by git).
  Keep it out of backups and screenshots, and rotate any key that has been shared. The `ANTHROPIC_API_KEY` value is
  not a valid Anthropic key format and is skipped automatically.
