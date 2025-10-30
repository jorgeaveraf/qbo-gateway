# syntax=docker/dockerfile:1.7

FROM python:3.11-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip uv

WORKDIR /app

COPY pyproject.toml .
COPY app app
COPY alembic alembic
COPY alembic.ini alembic.ini
COPY README.md README.md
COPY postman postman
COPY .env.example .env.example

RUN uv pip install --system --no-cache-dir .


FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH="/home/appuser/.local/bin:${PATH}"

RUN useradd -m appuser

WORKDIR /app

COPY --from=builder /usr/local /usr/local
COPY --from=builder /app /app

RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
