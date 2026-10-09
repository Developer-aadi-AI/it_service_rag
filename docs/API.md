# D Group Assistant — API reference

Base URL: wherever the service runs (local default `http://127.0.0.1:8000`).
Interactive docs: `/docs` (Swagger) and `/redoc` — disabled when `ENVIRONMENT=production`.
Machine-readable schema: [`docs/openapi.json`](openapi.json) (regenerate with `python scripts/export_openapi.py`).

All request and response bodies are JSON (UTF-8) unless stated otherwise. Timestamps are ISO 8601 in UTC.

## Authentication and limits

| What | How |
|---|---|
| Customer endpoints | No login. A chat is identified by its `session_id` (128-bit random) — treat it as a secret held by the browser. |
| Admin endpoints (`/admin/*`) | Header `X-Admin-Key: <ADMIN_API_KEY>`. Disabled (404) unless `ADMIN_API_KEY` is set. |
| CORS | Only origins in `CORS_ORIGINS` (comma separated). `*` is refused in production. |
| Rate limits | `RATE_LIMIT_PER_MINUTE` requests per IP per minute (default 60), and `RATE_LIMIT_SESSIONS_PER_HOUR` new chats per IP per hour (default 30). Exceeding returns `429` with `Retry-After`. Health, docs and widget files are exempt. |
| Body size | Requests larger than `MAX_REQUEST_BYTES` (default 64 KB) return `413`. |
| Capacity | More than `MAX_ACTIVE_SESSIONS` chats in memory → `POST /sessions` returns `503` with `Retry-After`. |
| Correlation | Send `X-Request-ID` (8–64 chars `[A-Za-z0-9._-]`) or one is generated; it is echoed in the response and in every log line. |

## Errors

Every error has the same shape (no stack traces or internal details):

```json
{"detail": "Invalid request", "errors": [{"field": "email", "message": "value is not a valid email address: ..."}]}
```

| Status | When |
|---|---|
| 404 | Unknown chat session (`Chat session not found. Please start a new chat.`), or admin endpoints disabled |
| 401 | Missing/invalid `X-Admin-Key` |
| 409 | Message sent to a chat that has ended (`This chat has ended. Please start a new chat.`) |
| 413 | Request body too large |
| 422 | Validation error (`errors` lists each field), e.g. empty message, invalid email, rating not Great/OK/Poor, email to a different address than the one given in the chat |
| 429 | Rate limit, or the chat's email limit (`EMAIL_MAX_PER_SESSION`) reached |
| 500 | Unexpected error (logged with the request id; generic message to the client) |
| 503 | Starting up, too many active chats, lead storage unavailable, or email not configured |

---

## Chat sessions

### `POST /sessions` — start a chat (201)

Request (all optional):

```json
{"name": "Priya Sharma", "email": "priya@example.com", "sound_enabled": true}
```

`email` enables the summary / follow-up emails when the chat ends. Response:

```json
{
  "session": {
    "session_id": "4f3c…(32 hex)", "status": "active", "flow": null, "close_reason": null,
    "attempts_remaining": 3, "irrelevant_limit": 3, "sound_enabled": true,
    "idle_check_after_seconds": 120, "idle_close_after_seconds": 60, "message_count": 1
  },
  "message": {"seq": 1, "role": "assistant", "kind": "greeting", "content": "Hello! I'm D Group's virtual assistant…",
              "created_at": "2026-10-08T09:30:00+00:00", "sources": []}
}
```

### `POST /sessions/{session_id}/messages` — send a message

```json
{"message": "How much does the Shopify Gold plan cost?"}
```

Response (`ChatResponse`):

```json
{
  "session": { "...": "SessionState as above" },
  "message": {"seq": 3, "role": "assistant", "kind": "message",
              "content": "The Shopify packages price varies based on the plan you have. Our Silver Plan begins at $3,999 and Gold Plan begins at $5,999. … [1]\n\nLet me know if you'd like more details.",
              "created_at": "…", "sources": [{"number": 1, "title": "Shopify Packages", "url": "https://webstore.vgroup.net/shopify-pricing/"}]},
  "status": "answered",
  "sources": [{"number": 1, "title": "Shopify Packages", "url": "https://webstore.vgroup.net/shopify-pricing/",
               "source_type": "pricing", "platform": "", "category": "Pricing", "snippet": "…"}],
  "recommendations": {
    "primary": {"name": "Shopify Packages", "url": "https://webstore.vgroup.net/shopify-pricing/", "kind": "package",
                "category": "Pricing", "description": "…", "cta": "Buy Now"},
    "related": [{"name": "Shopify Development Services", "…": "…"}, {"name": "Shopify App Development", "…": "…"}]
  },
  "action": null,
  "notification": {"sound": "message_received"},
  "debug": {"sentiment": "neutral", "search_query": "How much does the Shopify Gold plan cost?", "top_score": 1.0, "generator": "llm"}
}
```

| Field | Values |
|---|---|
| `status` | `answered` (grounded answer with `[n]` citations) · `needs_team` (no answer in the knowledge base — hand-off, never "I don't know") · `off_topic` (outside D Group; counts toward the limit) · `smalltalk` · `contact_prompt` (user asked for the team) · `lead_confirmation` · `continue` · `session_end` · `error` |
| `action` | `null`, `{"type": "contact_form", "fields": [...], "submit_endpoint": "/contact", "reason": "project_request" \| "information_request" \| "contact_request" \| "frustration" \| "error"}` or `{"type": "feedback", "options": ["Great", "OK", "Poor"]}` |
| `notification.sound` | `message_received` · `idle_check` · `session_ended` · `null` (sound off) |
| `debug` | Only outside production |

Message rules: 1–1000 characters (`MAX_MESSAGE_CHARS`), whitespace collapsed.

### `GET /sessions/{session_id}/messages?after=<seq>` — poll

Returns messages with `seq > after` plus the session state. Polling also runs the idle check, so the
"Are you still there?" message and the inactivity close arrive here. Poll every few seconds.

```json
{"session": {"…": "…"}, "messages": [{"seq": 9, "role": "assistant", "kind": "idle_check", "content": "Are you still there? …"}],
 "notification": {"sound": "idle_check"}}
```

Message `kind`s: `greeting`, `message`, `contact_prompt`, `form_submission`, `lead_confirmation`, `idle_check`,
`session_end`, `feedback_ack`.

### `GET /sessions/{session_id}` — session state
### `PATCH /sessions/{session_id}/preferences` — `{"sound_enabled": false}`
### `POST /sessions/{session_id}/close` — user closes the chat (idempotent; returns a `ChatResponse` with the feedback action)

## Leads and contact

### `POST /sessions/{session_id}/contact` — contact form inside a chat (201)

```json
{"name": "Ravi Kumar", "email": "ravi@example.com", "phone": "+91 98765 43210", "company": "Acme",
 "message": "We'd like a quote for a Shopify Gold package"}
```

Validation: name 2–100 chars (no digits/markup), valid email, phone optional (7+ digits), company ≤ 120,
message 5–2000. Response: `{"lead_id": "…", "session": {…, "flow": "awaiting_anything_else"}, "message": {"content": "Thank you, Ravi! We've received your details, and the relevant D Group team will connect with you shortly. Is there anything else I can help you with?"}}`.
The next user message "no" closes politely; "yes" or a new question continues.

### `POST /contact` — standalone form (201): same body, no session.

## Feedback

### `POST /sessions/{session_id}/feedback` — `{"rating": "Great" | "OK" | "Poor", "comment": "optional"}`
Allowed after the chat has ended. Returns `{"rating": "Great", "message": {…"kind": "feedback_ack"}}`.

### `GET /feedback/{token}?rating=Great` — one-click link used in emails
Signed with `APP_SECRET_KEY`; returns a small HTML thank-you page (400 for invalid links).

## Transcripts and email

### `GET /sessions/{session_id}/transcript` — `text/plain` download
`Content-Disposition: attachment; filename="dgroup-chat-transcript-YYYYMMDD-HHMM-<id>.txt"`. Contains the date,
start/end time, UTC timestamps for each message, both sides of the conversation and source links.

### `POST /sessions/{session_id}/emails` (202)

```json
{"type": "transcript" | "summary" | "followup" | "feedback", "email": "optional — defaults to the chat's address"}
```

A chat can only email the address its customer provided (first address wins) and at most
`EMAIL_MAX_PER_SESSION` times. Emails are sent in the background.

## Stateless question

### `POST /ask`

```json
{"question": "How many support hours are included annually?", "top_k": 5, "similarity_threshold": 0.55}
```

Returns `{request_id, answer, status, sources, action, off_topic, request_kind, debug}` (same meanings as above).

## System

| Endpoint | Purpose |
|---|---|
| `GET /health` | Status (`ok`/`degraded`), version, environment, answer mode, index size, thresholds, component `checks` (vector_index, database, email, analytics, llm), active sessions |
| `GET /health/live` | Liveness: `{"status": "ok"}` while the process runs |
| `GET /health/ready` | Readiness: 200 when the index and database are usable, else 503 |

## Admin

### `GET /admin/analytics?days=7&top=10` (JSON) · `GET /admin/analytics.txt?days=7` (text)
Requires `X-Admin-Key`. See [ANALYTICS.md](ANALYTICS.md) for the fields.

## Widget

`GET /widget/` serves the embeddable chat widget (static HTML/JS/CSS, strict CSP). `/` redirects there.
To embed on another site: `<script src="https://<assistant-host>/widget/widget.js" data-api="https://<assistant-host>"></script>`
together with the widget's HTML/CSS, and add the site's origin to `CORS_ORIGINS`.
