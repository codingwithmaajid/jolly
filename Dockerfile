FROM python:3.12-slim

# Production defaults; Render and the AWS box both honour $PORT.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PORT=8000 \
    WEB_CONCURRENCY=1

WORKDIR /app

RUN pip install --no-cache-dir uv \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin jolly \
    && mkdir -p /data \
    && chown jolly:jolly /data /app

# Dependencies first so code edits don't bust the layer cache.
COPY --chown=jolly:jolly pyproject.toml uv.lock .python-version ./
USER jolly
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=jolly:jolly . .
RUN uv sync --frozen --no-dev

EXPOSE 8000

# Python-only probe (the slim image has no curl). Render ignores HEALTHCHECK; Docker on EC2 uses it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["sh", "-c", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:${PORT:-8000}/health', timeout=4)\""]

# Keep WEB_CONCURRENCY=1 while the DB is SQLite: every worker seeds and builds the
# embedding index at startup, and SQLite has a single writer.
CMD ["sh", "-c", "exec /app/.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --proxy-headers --forwarded-allow-ips='*'"]
