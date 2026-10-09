# D Group AI assistant - production image (CPU).
FROM python:3.13-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf-cache

WORKDIR /app

# CPU-only torch keeps the image small; then the rest of the runtime deps.
COPY requirements.txt constraints.txt ./
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch && \
    pip install -r requirements.txt -c constraints.txt

COPY app ./app
COPY Database ./Database
COPY .env.example ./

# Bake the embedding model and the vector index into the image so containers start fast
# and work offline. Re-build the image when the dataset changes.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-base-en-v1.5')" && \
    python -m app.ingestion.pipeline

# Runtime data (SQLite, transcripts, email outbox) lives on a volume.
ENV HF_HUB_OFFLINE=1 \
    ENVIRONMENT=production \
    LOG_FORMAT=json \
    DATABASE_PATH=/data/dgroup.db \
    TRANSCRIPTS_DIR=/data/transcripts \
    EMAIL_OUTBOX_DIR=/data/outbox

RUN useradd --create-home --uid 10001 dgroup && mkdir -p /data && chown -R dgroup /data /app/storage
USER dgroup
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status == 200 else 1)"

# One worker: chat sessions live in process memory (see docs/DEPLOYMENT.md).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-server-header"]
