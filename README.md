# D Group AI Customer Support & Sales Assistant

AI customer-support and sales assistant for D Group, grounded in the supplied website data
(`Database/final_dataset.json`, 134 pages). It answers questions with citations, keeps multi-turn context,
recommends services, captures leads, handles frustrated users, off-topic and idle chats, produces transcripts
and emails, collects feedback, and reports analytics.

| Phase | Scope |
|---|---|
| 1 | Data ingestion → chunking → embeddings → vector store → hybrid retrieval → grounded answers with sources → FastAPI `/ask` |
| 2 | Sessions & memory, follow-ups, recommendations, sentiment, lead/contact flow, 3-attempt rule, idle sessions, transcripts, email, feedback, notification sounds |
| 3 | Evaluation harness, end-to-end tests, analytics, observability, security hardening, performance, API docs, deployment, demo, final audit |

## Quick start

```bash
pip install -r requirements-dev.txt -c constraints.txt
cp .env.example .env               # optional: add one LLM key (GROQ_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY)
python -m app.ingestion.pipeline   # build the index (first run ~4 min on CPU, then cached)
uvicorn app.main:app --reload      # chat widget: http://127.0.0.1:8000/widget/   API docs: /docs
```

| Command | Purpose |
|---|---|
| `pytest -q` | full test suite (offline, deterministic) |
| `python -m app.evaluation [--check]` | RAG evaluation → `reports/evaluation/latest.md` (+ history.csv); `--check` fails below thresholds |
| `python -m app.demo` | 10 demo conversations with automatic checks |
| `python -m app.analytics --days 7` | analytics summary (also `GET /admin/analytics`) |
| `docker compose up --build` | production-like container (see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)) |
| `notebooks/dgroup_rag_prototype.ipynb` | prototype notebook + Gradio app |

## Documentation

| Document | Contents |
|---|---|
| [docs/API.md](docs/API.md) | every endpoint, request/response schemas, errors, auth, limits ([openapi.json](docs/openapi.json)) |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | local setup, production config, Docker, scaling, operations, logging |
| [docs/EVALUATION.md](docs/EVALUATION.md) | evaluation dataset, metrics, latest results (offline and LLM) |
| [docs/SECURITY.md](docs/SECURITY.md) | security review: findings, fixes, tests, residual risks |
| [docs/ANALYTICS.md](docs/ANALYTICS.md) | recorded events, privacy, summary fields |
| [docs/DEMO.md](docs/DEMO.md) | demo script (10 scenarios); recorded run in [docs/DEMO_RUN.md](docs/DEMO_RUN.md) |
| [docs/REQUIREMENTS_AUDIT.md](docs/REQUIREMENTS_AUDIT.md) | CLAUDE.md requirements checklist (DONE / PARTIAL / NOT DONE) |

## Architecture

```
Browser widget (/widget) or Gradio prototype
        │  REST over HTTPS
        ▼
FastAPI ─ middleware: request id + access log → security headers → body limit → rate limit → CORS
  ├─ /ask (stateless) ───────────────────────────────────────────────┐
  ├─ /sessions/* ──► ConversationManager                             │
  │                   ├─ sentiment (rule-based) + emotional-clause filter
  │                   ├─ flows: contact request, "anything else?", goodbye, 3-attempt rule, idle check/close
  │                   ├─ FollowUpRewriter (LLM or heuristics)        │
  │                   ├─ RecommendationEngine (catalog of real service pages)
  │                   └─ LeadService · FeedbackService · EmailService (file|SMTP, background) · transcripts
  │                                                                  ▼
  │                                                         RAGPipeline
  │                       ├─ intents: small talk, unclear input, overview, company facts, contact details
  │                       ├─ Retriever: bge-base embeddings (LRU cache) + BM25 over Chroma, top-k, thresholds
  │                       ├─ LLMGenerator (Anthropic | Groq | OpenAI | Gemini + circuit breaker) or offline generator
  │                       └─ guards: citations required, blog-only offers → team, prompt-leak check
  ├─ /admin/analytics (X-Admin-Key) ◄── EventRecorder → SQLite (events, leads, feedback, email_log)
  └─ /health · /health/live · /health/ready
```

```
app/
  config.py              all settings (env / .env) + production safety checks
  logging_config.py      request/session ids, text|JSON logs, secret + PII redaction
  ingestion/             load, validate, clean, derive metadata, chunk, build index
  embeddings/ vectorstore/ retrieval/   bge embeddings (+LRU cache), Chroma|NumPy, hybrid BM25 retrieval
  llm/base.py            providers, key-format checks, circuit breaker
  rag/                   prompts, intents, generators, humanize, contact info, pipeline
  conversation/          sessions, manager, follow-ups, intents, idle monitor
  recommendations/ sentiment/ leads/ feedback/ email/ transcripts/ storage/ analytics/
  evaluation/            dataset loader + repeatable harness (python -m app.evaluation)
  api/                   health + ask, sessions, leads, feedback, transcripts, admin, middleware
  static/widget/         production chat widget (HTML/CSS/JS, notification sounds)
  ui/gradio_app.py       Gradio prototype
  demo.py                demo conversations (python -m app.demo)
tests/                   unit, integration, API, security, analytics, end-to-end, evaluation + eval/eval_set.json
```

## The supplied data

134 records with `page_id, url, title, text, source_type, platform` (case studies, blog posts, services,
products, about, pricing). CLAUDE.md's `service / category / description / keywords / CTA` fields are not in
the data; they are **derived** from the content (nothing invented). Cleaning: 8 `UNKNOWN` types re-derived
from URLs, 22 title-only portfolio entries kept as minimal records, 1 access-denied page dropped, repeated CTA
blocks stripped, 42 FAQ pairs extracted.

**Conflicts in the data (please confirm with D Group):** US head office "Cranbury, NJ" (Careers) vs
"East Windsor, NJ" (Contact Us, Privacy Policy); founding "1999" (About) vs "Since 2007" (Web Development);
clients "500+", "350+" and "300+" on different pages. The assistant answers from the cited page and prefers the
Contact Us page for contact details.

## Answer modes

`LLM_PROVIDER=auto` uses the first provider whose key looks valid (Anthropic `sk-ant-…`, Groq `gsk_…`,
OpenAI `sk-…`, Gemini `AIza…`); malformed keys are skipped with a warning (the value is never logged).
Default models: Anthropic `claude-opus-5-5`, Groq `openai/gpt-oss-120b`, OpenAI `gpt-4o-mini`, Gemini
`gemini-2.5-flash` (`LLM_MODEL` overrides). A circuit breaker pauses a failing provider (credential errors
1 h, repeated errors 60 s) and the assistant keeps answering offline. Without any key it runs fully offline,
quoting cleaned website sentences.

## Known limitations

- Sessions, rate-limit counters and the idle monitor live in process memory → run one worker (or add Redis).
- Sentiment is a rule-based English lexicon (no sarcasm detection).
- Offline answers quote marketing copy; some flattened tables (e.g. BigCommerce timelines) answer poorly offline.
- Grounding metrics are lexical approximations; LLM paraphrases can be flagged or missed.
- On Groq's free tier, rate limits add seconds of latency under load (the SDK retries with backoff).
- Real SMTP delivery and the widget in a physical browser were not exercised here (SMTP mocked; widget run in jsdom).
