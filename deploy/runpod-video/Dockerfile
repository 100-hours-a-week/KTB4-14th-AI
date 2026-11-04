# Single RunPod CPU Pod: private PostgreSQL, API, supervised sequential worker.
FROM postgres:17-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.9.30 /uv /usr/local/bin/uv
ENV DEBIAN_FRONTEND=noninteractive UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH" \
    PGDATA=/data/postgres VIDEO_WORKSPACE=/data/work VIDEO_STATE_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv ffmpeg fonts-noto-cjk supervisor openssh-server ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home audigo
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --extra video --no-dev --no-install-project --python /usr/bin/python3
COPY ai_service ./ai_service
COPY scripts ./scripts
RUN uv sync --frozen --extra video --no-dev --python /usr/bin/python3
COPY deploy/runpod-video ./deploy/runpod-video
RUN chmod +x deploy/runpod-video/entrypoint.sh && usermod -a -G audigo postgres
COPY deploy/runpod-video/initdb.sh /docker-entrypoint-initdb.d/10-audigo.sh
EXPOSE 8000 22
STOPSIGNAL SIGTERM
ENTRYPOINT ["/app/deploy/runpod-video/entrypoint.sh"]
