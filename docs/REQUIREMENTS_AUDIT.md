# Requirements audit (CLAUDE.md → implementation)

Status: **DONE** = implemented and tested · **PARTIAL** = implemented with a stated gap · **NOT DONE**.

## Source of truth & data

| Requirement | Status | Evidence |
|---|---|---|
| Inspect supplied data before implementing | DONE | Schema/field analysis in README "The supplied data"; loader decisions documented in `app/ingestion/loader.py` |
| Do not invent services/policies/pricing/FAQs | DONE | Answers only from retrieved pages; citation guard; blog-only offers routed to team; hallucination probes (eval) |
| Preserve useful metadata | DONE | page_id, url, title, source_type, platform, category, description, keywords, CTAs, platforms, content quality on every chunk |
| No-answer handled without hallucinating | DONE | `needs_team` hand-off ("great idea" + form for projects); eval no-answer routing 1.0 offline |
| Expected fields (service, category, description, keywords, CTA, source_type, platform) | DONE (derived) | Data has only source_type/platform; others derived from content (flagged as a data difference, nothing invented) |

## Architecture & RAG

| Requirement | Status | Evidence |
|---|---|---|
| Modular: LLM, embeddings, vector DB, email, UI swappable | DONE | interfaces in `llm/`, `embeddings/`, `vectorstore/`, `email/senders.py`, `services.py` wiring |
| Loading/validation, chunking, metadata, embeddings, vector storage | DONE | `ingestion/`, `embeddings/`, `vectorstore/`; tests in `test_ingestion.py`, `test_retrieval.py` |
| Similarity search, configurable top-k and threshold | DONE | `TOP_K`, `SIMILARITY_THRESHOLD`, `DOMAIN_THRESHOLD`; per-request overrides on `/ask` |
| Context construction, grounded prompting, citations | DONE | tagged `<context>/<source>`; JSON answers with `[n]`; invalid markers removed; uncited answers rejected |
| No-answer and off-topic handling | DONE | pipeline decision flow + unclear-input rule; eval off-topic accuracy 1.0 |
| Retrieval independently testable | DONE | `Retriever` tested without LLM; harness retrieval metrics (hit@5 1.0, MRR 0.99) |

## Conversation

| Requirement | Status | Evidence |
|---|---|---|
| Multi-turn, session memory, follow-ups, context-aware answers | DONE | `ConversationManager`, `FollowUpRewriter`; eval follow-up recall 1.0 offline |
| Primary + secondary recommendations | DONE | `RecommendationEngine` over real service pages; `test_recommendations.py` |
| Graceful closing | DONE | goodbye / "no" after "anything else?" / close endpoint → feedback prompt + summary email |
| Session state controlled and configurable | DONE | history turns, stored message cap, retention, max active sessions (env) |

## Sentiment, irrelevant input, idle

| Requirement | Status | Evidence |
|---|---|---|
| Sentiment-aware responses (acknowledge, reassure, help, clarify) | DONE | rule-based analyzer; single non-repeated acknowledgement; clarifying question for vague complaints; team offer on repeated frustration |
| Avoid robotic/excessive empathy | DONE | one short acknowledgement, not on consecutive turns (tested) |
| Irrelevant inputs: inform, track, show remaining, end after 3, configurable | DONE | `IRRELEVANT_LIMIT`; tests + demo scenario 6 |
| Idle: check → wait → close → explain → email | DONE | background monitor + lazy polling; `IDLE_*` settings; demo scenario 8; widget run (jsdom) |
| No scattered hard-coded timeouts | DONE | all timings in `config.py` |

## Leads, transcripts, email, notifications

| Requirement | Status | Evidence |
|---|---|---|
| Contact form → confirm → team will connect → anything else? → close/continue | DONE | session contact flow; tests; demo scenario 7; widget run |
| Validate contact data before storage | DONE | `LeadInput` (lengths, email, phone, name characters, control chars) |
| Transcripts: UTC timestamps, date header, `.txt` | DONE | `transcripts/builder.py`; download endpoint |
| Transcript / summary / follow-up / feedback emails | PARTIAL | All four implemented, tested with an in-memory mailbox, a file outbox and a mocked SMTP server. Delivery through a real SMTP server was not tested. |
| Feedback Great / OK / Poor | DONE | chat + signed email links |
| No internal details in customer emails | DONE | template tests scan for internal terms |
| Notification sounds + enable/disable, no backend effect | DONE | widget + Gradio (Web Audio), API sound hints; actual audio not listened to (environment has no speakers) |

## FastAPI, logging, analytics

| Requirement | Status | Evidence |
|---|---|---|
| Separated concerns (routes, RAG, LLM, embeddings, store, sessions, recs, leads, sentiment, email, transcript, logging, analytics, config) | DONE | package layout (README) |
| `/ask` wraps the core pipeline | DONE | `api/routes.py` |
| Secrets from environment only | DONE | `.env` ignored, `.env.example` empty secrets (tested) |
| Analytics: top questions, no-answer rate, recommendations, session stats, leads, feedback, errors | DONE | `analytics/`, `/admin/analytics`, CLI; `test_analytics.py` |
| Avoid sensitive-data logging | DONE | PII masking + secret scrubbing in every log line; analytics without contact details (tested) |

## Testing

| Requirement | Status | Evidence |
|---|---|---|
| Retrieval quality, grounding, citations, no-answer, off-topic | DONE | unit tests + evaluation harness |
| Multi-turn, recommendations, sentiment, irrelevant limits, idle, contact, transcript, email, API edge cases | DONE | `test_conversation.py`, `test_api_sessions.py`, `test_transcripts_email.py`, `test_security.py`, `test_e2e.py` |
| Evaluation set built from the data; quality not only HTTP 200 | DONE | `tests/eval/eval_set.json` v2 (facts verified against pages), `python -m app.evaluation` |

## Code quality & security

| Requirement | Status | Evidence |
|---|---|---|
| Type hints, centralized configuration, explicit error handling | DONE | throughout |
| Never expose secrets, stack traces, internal prompts or metadata | DONE | generic errors, debug only outside production, prompt-leak guard (tests) |
| `.env.example` | DONE | all settings documented |

## Definition of Done

| Item | Status |
|---|---|
| Ingestion, retrieval, grounded answers with sources, multi-turn memory, recommendations, lead capture, sentiment, no-answer/off-topic, idle, transcripts, email follow-up, feedback, logging, analytics | DONE |
| Tested/documented FastAPI endpoints | DONE (`docs/API.md`, `docs/openapi.json`) |
| Working end-to-end chat → API → RAG → answer → citation → lead flow | DONE (`test_e2e.py`, widget run in jsdom against the live server, demo script) |

## Gaps that remain (PARTIAL items)

1. **Real email delivery.** SMTP is implemented and tested against a mocked server. No real mail server was used. Configure `SMTP_*` and send one test email before go-live.
2. **LLM-mode quality depends on the provider.** All evaluation thresholds pass in offline mode. In LLM mode (Groq `openai/gpt-oss-120b`), measured results are in `docs/EVALUATION.md`. The free tier's 8,000 tokens per minute limit throttles evaluation runs.
3. **Horizontal scaling.** Sessions are held in memory, so the service must run as a single worker. Moving the session store to Redis would remove this limit.

Nothing in CLAUDE.md is NOT DONE.
