#!/bin/bash
set -euo pipefail
umask 077
: "${AUDIGO_API_TOKEN:?Set a backend service token}"
: "${RUNPOD_S3_ACCESS_KEY_ID:?Set RunPod S3 credentials}"
: "${RUNPOD_S3_SECRET_ACCESS_KEY:?Set RunPod S3 credentials}"
: "${RUNPOD_STORAGE_VOLUME_ID:?Set media volume ID}"
: "${RUNPOD_STORAGE_REGION:?Set media volume region}"
: "${OPENAI_API_KEY:?Set OpenAI API key}"
# Runtime PostgreSQL passwords live only on the private persistent service volume.
mkdir -p /data/postgres /data/config /data/backups /data/logs /data/work /run/postgresql /run/sshd
chmod 700 /data/config /data/backups
chown postgres:postgres /data/postgres /run/postgresql
chown audigo:audigo /data/work /data/logs
python3 /app/deploy/runpod-video/configure.py
export POSTGRES_USER=postgres POSTGRES_DB=audigo_video
export POSTGRES_PASSWORD_FILE=/data/config/postgres-password
# Reuse official image initialization, then immediately stop its server before supervisor takes ownership.
if [ ! -s /data/postgres/PG_VERSION ]; then
  /usr/local/bin/docker-entrypoint.sh postgres -c listen_addresses=127.0.0.1 &
  bootstrap_pid=$!
  trap 'kill -TERM "$bootstrap_pid" 2>/dev/null || true' EXIT
  for i in $(seq 1 120); do
    if gosu postgres psql -h /var/run/postgresql -U postgres -d audigo_video -c 'SELECT 1' >/dev/null 2>&1; then break; fi
    kill -0 "$bootstrap_pid"
    sleep 1
  done
  # Wait for final server (entrypoint temporarily starts another server during init).
  until [ "$(head -1 /data/postgres/postmaster.pid 2>/dev/null || true)" = "$bootstrap_pid" ]; do
    kill -0 "$bootstrap_pid"; sleep 1
  done
  kill -TERM "$bootstrap_pid"
  wait "$bootstrap_pid" || true
  trap - EXIT
fi
if [ -n "${PUBLIC_KEY:-}" ]; then
  mkdir -p /root/.ssh
  printf '%s\n' "$PUBLIC_KEY" > /root/.ssh/authorized_keys
  chmod 700 /root/.ssh; chmod 600 /root/.ssh/authorized_keys
  ssh-keygen -A
  printf '\nPasswordAuthentication no\nPermitRootLogin prohibit-password\n' > /etc/ssh/sshd_config.d/audigo.conf
fi
exec /usr/bin/supervisord -n -c /app/deploy/runpod-video/supervisord.conf
