# Deployment guide

Nothing in this repository creates cloud resources; the steps below are for whoever operates the service.

## 1. Requirements

- Python 3.11+ (tested on 3.13), ~2 GB RAM (embedding model + index), CPU is fine (no GPU needed).
- Disk: ~1.5 GB for dependencies + model; the index (`storage/chroma`) is ~20 MB.
- An LLM API key is optional (Anthropic, Groq, OpenAI or Gemini). Without one the assistant answers offline
  from quoted website text.

## 2. Local setup

```bash
python -m venv .venv && . .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt -c constraints.txt   # runtime-only: requirements.txt
cp .env.example .env                                     # fill in what you need
python -m app.ingestion.pipeline                         # build the vector index (~4 min first time, then cached)
uvicorn app.main:app --reload                            # http://127.0.0.1:8000/widget/  and  /docs
pytest -q                                                # full test suite (offline, deterministic)
python -m app.evaluation                                 # evaluation report -> reports/evaluation/latest.md
python -m app.demo                                       # 10 demo conversations with checks
```

## 3. Production configuration

`ENVIRONMENT=production` refuses to start unless these are safe:

| Variable | Production value |
|---|---|
| `APP_SECRET_KEY` | random, ≥ 32 chars (`python -c "import secrets; print(secrets.token_urlsafe(48))"`) — signs feedback links |
| `CORS_ORIGINS` | the website origin(s), e.g. `https://webstore.vgroup.net` (never `*`) |
| `PUBLIC_BASE_URL` | the public `https://` URL of this service (used in email feedback links) |
| `ADMIN_API_KEY` | optional; ≥ 24 chars if set (enables `/admin/analytics`) |

Recommended:

```env
ENVIRONMENT=production
LOG_FORMAT=json
LOG_LEVEL=INFO
LLM_PROVIDER=auto              # first provider with a well-formed key; or name it explicitly
GROQ_API_KEY=...               # or ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY
EMAIL_BACKEND=smtp
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=...
SMTP_PASSWORD=...
EMAIL_FROM=D Group <no-reply@dgroup.example>
TEAM_NOTIFICATION_EMAIL=sales@dgroup.example
TRUST_PROXY_HEADERS=true       # only if behind a reverse proxy that sets X-Forwarded-For
```

Production also disables `/docs` and `/redoc`, hides `debug` fields from responses, and sends security headers
(`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, CSP for the widget).

## 4. Run with Docker

```bash
docker compose up --build        # builds the image, bakes the model + index, starts on :8000
```

- The image runs as a non-root user, offline (`HF_HUB_OFFLINE=1`), with JSON logs.
- Runtime data (SQLite leads/feedback/analytics, transcripts, email outbox) lives in the `/data` volume.
- `HEALTHCHECK` calls `/health/live`; use `/health/ready` for load-balancer readiness.
- Rebuild the image when `Database/final_dataset.json` changes (the index is baked in at build time).

## 5. Run without Docker

```bash
pip install -r requirements.txt -c constraints.txt
python -m app.ingestion.pipeline
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips="<proxy ip>"
```

Put a reverse proxy (nginx, Caddy, a cloud load balancer) in front for TLS. Example nginx location:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Request-ID $request_id;
    client_max_body_size 64k;
}
```

## 6. Scaling notes (important)

- **Run one worker process.** Chat sessions, rate-limit counters and the idle monitor live in process memory.
  Multiple workers would split a conversation across processes. To scale out, move sessions to Redis (implement
  `SessionStore`) and rate limiting to the proxy or Redis, then run several workers/instances.
- One worker handles concurrent chats: embedding and LLM calls run in a thread pool.
- Latency: ~0.05–1 s per message offline on CPU (embedding cache warms up); with an LLM add the provider's latency
  (Groq `openai/gpt-oss-120b` measured ~1–2 s per call when not rate limited).

## 7. Operations

| Task | Command |
|---|---|
| Rebuild the index after a data change | `python -m app.ingestion.pipeline --force` |
| Analytics report | `python -m app.analytics --days 7` or `GET /admin/analytics` |
| Evaluation after changes (CI gate) | `python -m app.evaluation --check` (exit code 1 if a metric drops below its threshold) |
| Pre-demo smoke test | `python -m app.demo` |
| Export API schema | `python scripts/export_openapi.py` |
| Back up data | copy `/data/dgroup.db` (SQLite; use `sqlite3 dgroup.db ".backup out.db"` while running) |

## 8. Logging

- One access-log line per request: method, path, status, latency, `req=<request id>`, `sess=<first 8 chars of session>`.
- Per chat turn: status, sentiment, rewrite flag, cited page ids, latency, PII-masked question.
- LLM calls: provider, model, latency; failures and circuit-breaker pauses at ERROR.
- Email/lead failures at ERROR with masked recipients.
- Secrets (configured keys, passwords, bearer tokens, `sk-…`/`gsk_…`/`AIza…` patterns) are scrubbed from every line;
  emails and phone numbers in user text are masked.
- `LOG_FORMAT=json` emits one JSON object per line: `ts, level, logger, request_id, session_id, message`.
