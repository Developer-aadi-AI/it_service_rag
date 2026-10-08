# V Group AI Customer Support & Sales Assistant

AI customer-support and sales assistant for V Group built on the supplied website data
(`Database/final_dataset.json`).

- **Phase 1** — data → ingestion → embeddings → vector store → hybrid retrieval → grounded answers
  with sources → FastAPI `/ask`.
- **Phase 2** — chat sessions with memory and follow-ups, service recommendations, sentiment-aware
  replies, lead capture and the contact flow, the 3-attempt irrelevant-input rule, idle sessions,
  transcripts, email (transcript / summary / follow-up / feedback), feedback, notification sounds.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env            # add one LLM key (optional, see below)
python -m app.ingestion.pipeline # build the index (first run ~4 min on CPU, then cached)
uvicorn app.main:app --reload    # http://127.0.0.1:8000/docs
pytest -q                        # 232 tests
python -m app.evaluation         # retrieval + end-to-end metrics
```

Prototype UI: open `notebooks/vgroup_rag_prototype.ipynb` and run all cells (Gradio at http://127.0.0.1:7860).
Emails are written to `storage/outbox/*.eml` by default (`EMAIL_BACKEND=file`); set `EMAIL_BACKEND=smtp` and the
`SMTP_*` variables to deliver real email.

### LLM providers
`LLM_PROVIDER=auto` picks the first provider with a key: `ANTHROPIC_API_KEY` (default model
`claude-opus-5-5`, low effort, server-side refusal fallback enabled), `GROQ_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY`. Override the model with `LLM_MODEL`.
**Without any key the system runs in offline extractive mode**: answers are built from sentences
taken verbatim from the retrieved pages, cleaned of headings/button labels, composed into a short
paragraph with grouped citations and a friendly follow-up line (`app/rag/humanize.py`). Contact
questions are answered from fields parsed out of the Contact Us page (`app/rag/contact_info.py`),
plan prices from the pricing tables, and company-overview questions ("what do you guys do?") from the
About/Services pages. For fully conversational wording, configure an LLM key.

## The supplied data (what was found)

| | |
|---|---|
| Records | 134 web pages (WordPress site + scraped pages + portfolio) |
| Fields | `page_id, url, title, text, source_type, platform` |
| source_type | case-study 42, blog-post 32, service 24, app-product 19, UNKNOWN 8, about 5, pricing 3, home 1 |
| Text | flattened (no newlines), median ~4k chars |

**Differences from CLAUDE.md's expected fields:** the data has no `service`, `category`,
`description`, `keywords` or `CTA` fields. They are *derived* from the supplied content
(category from URL/source type, description = first sentences, keywords = TF-IDF terms,
CTAs = CTA phrases that actually appear on the page). Nothing is added from outside the data.

**Data issues handled**
- 8 `UNKNOWN` source types re-derived from the URL (policy, contact, partner, service, blog-index, product-category).
- 22 portfolio entries contain only the client name → kept as minimal records
  ("X is a client project listed in V Group's portfolio on <platform>"), penalised in answer selection.
- 1 WordPress "not allowed to access this page" preview → dropped.
- Repeated site-wide CTA block and contact-form field labels → stripped (CTA kept as metadata).
- 42 numbered FAQ pairs extracted as dedicated chunks (verbatim from pages).
- **Conflict flagged:** the Careers page lists the US head office as *Cranbury, NJ*; the Contact Us
  page and Privacy Policy list *379 Princeton Hightstown Road, East Windsor, NJ 08520*. The prompt
  tells the LLM to prefer the Contact Us page for contact details. Please confirm the correct address.

## Architecture

```
app/
  config.py               all settings (env vars / .env)
  ingestion/loader.py     load + validate + clean + derive metadata + FAQ extraction
  ingestion/chunker.py    sentence-aware chunks (900 chars, 150 overlap) + FAQ chunks
  ingestion/pipeline.py   build/reuse index (fingerprint manifest)  -> python -m app.ingestion.pipeline
  embeddings/base.py      sentence-transformers (BAAI/bge-base-en-v1.5) | hashing (tests)
  vectorstore/base.py     Chroma (persistent, cosine) | NumPy (exact)
  retrieval/lexical.py    BM25, normalised to 0..1 term coverage
  retrieval/retriever.py  hybrid search, top-k, threshold, per-page diversity
  llm/base.py             Anthropic | Groq | OpenAI | Gemini clients behind one interface
  rag/prompts.py          grounded system prompt, JSON schema, context builder, fixed replies
  rag/intent.py           small talk + business/project-intent heuristics
  rag/generator.py        LLM generator, extractive generator, triage
  rag/pipeline.py         decision flow -> RAGResponse
  evaluation.py           metrics on tests/eval/eval_set.json
  # ---- Phase 2 ----
  conversation/models.py  Session + Message (UTC timestamps, bounded history)
  conversation/store.py   in-memory, thread-safe session store (swap for Redis/DB)
  conversation/manager.py ConversationManager: memory, flows, limits, idle, closing
  conversation/followup.py follow-up -> standalone query (LLM rewrite or heuristics)
  conversation/intents.py contact requests, yes/no after "anything else?"
  conversation/idle.py    background idle monitor (asyncio task)
  sentiment/analyzer.py   rule-based sentiment + short acknowledgements + emotional-clause filter
  recommendations/engine.py service catalog (supplied pages) + primary/related recommendations
  leads/service.py        contact-form validation + lead storage + team notification hook
  storage/database.py     SQLite: leads, feedback, email log (parameterised SQL)
  transcripts/builder.py  .txt transcript + customer-facing summary
  email/                  senders (file | smtp | disabled), templates, workflows
  feedback/service.py     Great / OK / Poor, signed one-click email links
  services.py             wires all Phase 2 services around the pipeline
  api/                    routes.py (/health, /ask), sessions.py, leads.py, feedback.py, transcripts.py
  ui/gradio_app.py        Gradio prototype (used by the notebook)
frontend/notification-sounds.js   reusable Web Audio notification sounds + mute toggle
```

### RAG decision flow

1. **Small talk** (hi / thanks / bye) → friendly fixed reply (not counted as off-topic).
2. **Hybrid retrieval**: `score = cosine(bge) + 0.25 × BM25 coverage` (capped at 1). Dense alone
   missed exact-term questions (e.g. "phone number" → Contact Us page).
3. `score ≥ SIMILARITY_THRESHOLD (0.55)` → context is built (numbered per page) and the generator answers.
   The LLM must return JSON `{status, answer, citations, request_kind}`; answers without valid
   citations are rejected; citation markers to unknown sources are removed.
4. `DOMAIN_THRESHOLD (0.52) ≤ score < 0.55`, or the generator found no supported answer →
   **team hand-off**: the user is *not* told "no answer found". Project ideas get
   *"That sounds like a great idea! …"*, information requests get a team reply; both return a
   `contact_form` action (POST `/contact`).
5. Below the domain threshold → **off-topic** reply (`off_topic: true`, for Phase 2 attempt counting).
   With an LLM configured, a triage call can still rescue clearly business-related questions.

Failures: LLM errors/refusals/invalid JSON fall back to extractive answers; any other exception
returns a generic message + contact form (no stack traces or internals are exposed).

### Threshold calibration
Measured on the supplied data with bge-base-en-v1.5 + hybrid scoring: off-topic questions top out
at ~0.50, in-domain no-answer questions score 0.54–0.81, answerable questions 0.57–1.00. Retrieval
score alone cannot separate "answerable" from "in domain but not covered", so that final decision is
made by the generator (LLM) or, offline, by sentence-level evidence + subject-coverage checks.
Re-calibrate if you change the embedding model.

## API

`POST /ask`
```json
{"question": "How many support hours are included annually?", "top_k": 5, "similarity_threshold": 0.55}
```
Response (`debug` is omitted when `ENVIRONMENT=production`):
```json
{"request_id": "…", "status": "answered",
 "answer": "You get 120 hours of expert service annually … [1]",
 "sources": [{"number": 1, "title": "Annual Support", "url": "https://webstore.vgroup.net/services/annual-store-support/",
              "source_type": "service", "platform": "", "category": "Services", "snippet": "…"}],
 "action": null, "off_topic": false, "request_kind": "information_request",
 "debug": {"top_score": 0.97, "generator": "extractive", "mode": "extractive", "latency_ms": 120}}
```
`status` ∈ `answered | needs_team | off_topic | smalltalk | error`. `action` is a contact-form
descriptor when the team should follow up.

`POST /contact` — standalone contact form; validated lead stored in SQLite (team notified if configured).
`GET /health` — mode, index size, thresholds.

### Phase 2 endpoints

| Method & path | Purpose |
|---|---|
| `POST /sessions` | start a chat (`name`, `email`, `sound_enabled` optional) → session state + greeting |
| `GET /sessions/{id}` | session state: status, flow, attempts remaining, idle timings, sound preference |
| `POST /sessions/{id}/messages` | send a message → reply, status, sources, recommendations, action, notification |
| `GET /sessions/{id}/messages?after=<seq>` | poll for new messages (idle check, auto-close); runs the idle check lazily |
| `PATCH /sessions/{id}/preferences` | `{"sound_enabled": false}` mutes sound hints for this chat |
| `POST /sessions/{id}/close` | user closed the chat (idempotent) |
| `POST /sessions/{id}/contact` | contact/support form from the chat → confirmation + "anything else?" |
| `POST /sessions/{id}/feedback` | `{"rating": "Great" \| "OK" \| "Poor", "comment"?}` |
| `GET /feedback/{token}?rating=Great` | one-click feedback from emails (HMAC-signed link) |
| `GET /sessions/{id}/transcript` | `.txt` transcript download |
| `POST /sessions/{id}/emails` | `{"type": "transcript" \| "summary" \| "followup" \| "feedback", "email"?}` |

Errors: unknown session → 404, message to an ended chat → 409, validation → 422 (field-level errors), no
stack traces. Chat replies include `debug` (sentiment, search query, score) only outside production.

## Conversation behaviour (Phase 2)

- **Memory.** Each session stores its messages (UTC timestamps, capped by `SESSION_MAX_STORED_MESSAGES`);
  the last `SESSION_HISTORY_TURNS` user/assistant pairs are passed to the LLM.
- **Follow-ups.** "And how long does it take?" / "Does it work with Shopify?" are rewritten into standalone
  search queries (LLM rewrite when a key is set; otherwise the previous topic is prefixed, and "what about X?"
  substitutes X into the previous question). A rewrite is kept only if it retrieves at least as well, and
  "what about the weather?" is never rewritten.
- **Recommendations.** The catalog is built from the supplied service, hire-a-developer, Shopify-app,
  package and product pages (40 items; contact-form stubs and index pages excluded). *Primary* = a product
  named in the question, else the catalog page the answer was grounded on, else the most similar item
  (≥ `RECOMMENDATION_MIN_SIMILARITY`). *Related* = items close to the primary item and the question, without
  near-duplicates (Magento 1/2 variants) or other platforms. Returned in `recommendations`; a sentence is added
  to the answer when the user asks for a recommendation or describes a project.
- **Sentiment.** positive / neutral / negative / frustrated. Negative and frustrated messages get one short
  acknowledgement (not repeated on consecutive turns) followed by the actual answer; purely emotional clauses are
  dropped from the search query. A complaint with no business subject is not answered with a random page: it
  gets an offer to connect with the team (or a clarifying question when there is no topic yet). Repeated
  frustration (`SENTIMENT_ESCALATE_AFTER`) opens the contact form.
- **Contact flow.** Explicit requests ("talk to your sales team", "call me back", "get a quote") and team
  hand-offs return a `contact_form` action. After submission: confirmation, "the relevant team will connect with
  you shortly", "Is there anything else I can help you with?" → *no* closes politely (feedback prompt + summary
  email), *yes* or a new question continues. Leads record the recommended service as `interest`.
- **Irrelevant inputs.** Each off-topic/unclear message says it's outside supported V Group information and shows
  the attempts left; after `IRRELEVANT_LIMIT` (3) the chat ends gracefully. Small talk and in-domain questions
  without an answer never count.
- **Idle sessions.** After `IDLE_CHECK_AFTER_SECONDS` without activity: "Are you still there?"; after
  `IDLE_CLOSE_AFTER_SECONDS` more: the chat closes, explains why, and (if an email is known and
  `EMAIL_ON_IDLE_CLOSE`) sends a follow-up email with the transcript. Runs in a background monitor and lazily on
  polling.
- **Closing.** Goodbye / "no thanks" after "anything else?" / the close endpoint end the chat; the transcript is
  saved to `storage/transcripts/` and, when an email is known, a summary (with transcript and feedback links) is
  sent.

## Frontend contract (notification sounds)

The Gradio prototype plays sounds and has a sound toggle. For the production website widget:

- Load `frontend/notification-sounds.js`; call `vgPlay("sent")` when the user sends a message and
  `vgPlay(response.notification.sound)` for API responses (`message_received`, `idle_check`, `session_ended`,
  or `null`).
- The mute toggle calls `vgSound.setEnabled(on)` (remembered in localStorage) and
  `PATCH /sessions/{id}/preferences`; the API then returns `notification.sound = null` for that chat.
- Poll `GET /sessions/{id}/messages?after=<last seq>` every few seconds to receive the idle check and close.
- Render `action.type == "contact_form"` (fields are listed in the action) and `"feedback"` (Great/OK/Poor).

Frontend-specific work that remains: the production website widget itself (layout, branding, accessibility),
browser autoplay handling beyond the first user gesture, and choosing final sound assets if Web Audio tones are
not wanted.

## Evaluation (offline extractive mode, current data)

| Metric | Value |
|---|---|
| Retrieval hit@5 (32 answerable questions) | 1.00 |
| Retrieval MRR | 0.956 |
| End-to-end answer rate | 1.00 |
| Citation accuracy (cites a page containing the answer) | 0.938 |
| No-answer questions routed to team | 1.00 |
| Off-topic accuracy | 1.00 |

Known offline-mode limits (an LLM resolves these): wording is the website's own marketing copy rather
than a tailored reply, and questions phrased very differently from the site text may be handed to the
team (e.g. "Are there any job openings?").

## Known limitations
- Sessions live in memory: they are lost on restart and not shared between worker processes (use Redis or a
  database for multi-instance deployments). Leads, feedback and the email log are persisted in SQLite.
- Sentiment is rule-based (English lexicon); sarcasm and mixed messages can be misread.
- Without an LLM key, follow-up rewriting and answers use heuristics and quoted website copy.
- Idle detection is server-side; the client must poll (or the background monitor's messages are only seen on the
  next poll).
- Feedback links are signed with `APP_SECRET_KEY`; without it a random key is used and links break on restart.

## Phase 3 (next)
Analytics (top questions, no-answer rate, recommendations, sessions, leads, feedback, errors), structured
logging/metrics, LLM-mode evaluation with a real key, load/security hardening (rate limiting, auth for internal
endpoints, persistent session store), deployment (Docker, process manager, HTTPS) and the production widget.
