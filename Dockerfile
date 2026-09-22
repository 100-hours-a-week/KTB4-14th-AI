# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.9.30 /uv /bin/uv

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY ai_service ./ai_service
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12-slim AS runtime
WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --system fastapi \
    && useradd --system --gid fastapi --create-home fastapi

COPY --from=builder --chown=fastapi:fastapi /app/.venv ./.venv
COPY --from=builder --chown=fastapi:fastapi /app/ai_service ./ai_service

USER fastapi
EXPOSE 8000

ENTRYPOINT ["uvicorn", "ai_service.main:app", "--host", "0.0.0.0", "--port", "8000"]