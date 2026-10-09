# Analytics

Run `python -m app.analytics --days 7` (text) / `--json`, or call `GET /admin/analytics?days=7` with `X-Admin-Key`.

## What is recorded (SQLite `events` table)

| Event | Data (no names, emails, phones or lead messages) |
|---|---|
| `session_started` | whether an email was given |
| `message` | status, sentiment, latency, request kind, generator, top score, follow-up rewritten, question* |
| `ask` | same for the stateless `/ask` endpoint |
| `recommendation` | primary and related service names |
| `session_closed` | reason, duration, user messages, irrelevant count, lead captured, emailed |
| `lead_submitted` | interest (service), source, whether phone/company were given |
| `feedback` | rating, channel (chat/email), whether a comment was given |
| `email` | email type, sent/failed |
| `error` | component (`rag`, `llm`, `email`, `email_build`, `leads`, `sessions`, `api`) and exception type |

\* Question text is optional (`ANALYTICS_STORE_QUESTIONS`), lower-cased, PII-masked and truncated to 120 characters.
Leads, feedback and the email log live in their own tables (leads contain the contact details the customer submitted).

## Summary fields

| Section | Fields |
|---|---|
| `sessions` | started, closed, avg/median duration, avg user messages, close reasons, sessions with irrelevant input, ended by the irrelevant limit, ended by inactivity |
| `questions` | total, by status, answer rate, **no-answer rate** (team hand-offs / in-domain questions), off-topic rate, avg and p95 latency, **top questions**, sentiment mix |
| `recommendations` | shown, top primary services, top related services |
| `leads` | submitted, by source, by interest |
| `feedback` | responses, Great / OK / Poor, satisfaction (% Great), by channel |
| `emails` | sent, failed, by type |
| `errors` | total, by component, by type |

Example (text):

```
Sessions
  started 3 | closed 3 | avg duration 17.1s (median 16.5s) | avg user messages 3.7
  close reasons: {'idle_timeout': 3}
Questions
  total 9 | answered 100.0% | no-answer (team hand-off) 0.0% | off-topic 0.0%
  latency avg 499.2 ms | p95 3076.0 ms | sentiment {'neutral': 9}
  top questions:
     1. (3) how much does the shopify gold plan cost
...
```
