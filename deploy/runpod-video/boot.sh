#!/bin/bash
# RunPod base-image deployment: immutable source bundle + pinned dependency lock on /data.
set -euo pipefail
# Keep boot output on the volume; without SSH it is the only way to see a failed first start.
mkdir -p /data/logs
exec > >(tee -a /data/logs/boot.log) 2>&1
echo "boot started $(date -u +%FT%TZ)"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends python3 python3-venv ffmpeg fonts-noto-cjk supervisor
id audigo >/dev/null 2>&1 || useradd --uid 10001 --create-home audigo
usermod -a -G audigo postgres
ln -sfn /data/releases/current /app
install -m 755 /data/deploy/uv-linux-amd64 /usr/local/bin/uv
export UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
cd /app
uv sync --frozen --extra video --no-dev --python /usr/bin/python3
mkdir -p /docker-entrypoint-initdb.d
install -m 644 /app/deploy/runpod-video/initdb.sh /docker-entrypoint-initdb.d/10-audigo.sh
# The Pod runs this under `bash -lc`; the login shell drops the image's PostgreSQL bin directory from PATH.
export PATH="/app/.venv/bin:/usr/lib/postgresql/17/bin:$PATH" PGDATA=/var/lib/postgresql/data VIDEO_WORKSPACE=/data/work VIDEO_STATE_DIR=/data PYTHONUNBUFFERED=1
# Root-only runtime secrets were transferred over SSH, never added to the source bundle.
exec /app/.venv/bin/python /app/deploy/runpod-video/start.py
