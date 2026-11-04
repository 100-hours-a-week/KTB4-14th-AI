#!/bin/bash
set -euo pipefail
umask 077
: "${AUDIGO_API_TOKEN:?Set a backend service token}"
: "${RUNPOD_S3_ACCESS_KEY_ID:?Set RunPod S3 credentials}"
: "${RUNPOD_S3_SECRET_ACCESS_KEY:?Set RunPod S3 credentials}"
: "${RUNPOD_STORAGE_VOLUME_ID:?Set media volume ID}"
: "${RUNPOD_STORAGE_REGION:?Set media volume region}"
: "${OPENAI_API_KEY:?Set OpenAI API key}"
# PGDATA is on the Pod's local disk; /data/pg holds the durable base backups and WAL archive.
export PGDATA=${PGDATA:-/var/lib/postgresql/data}
mkdir -p /data/config /data/backups /data/logs /data/work /data/pg/base /data/pg/wal /run/postgresql /run/sshd "$PGDATA"
chmod 700 /data/config /data/backups "$PGDATA"
chown postgres:postgres "$PGDATA" /run/postgresql /data/pg /data/pg/base /data/pg/wal
chown audigo:audigo /data/work /data/logs
python3 /app/deploy/runpod-video/configure.py
export POSTGRES_USER=postgres POSTGRES_DB=audigo_video
export POSTGRES_PASSWORD_FILE=/data/config/postgres-password
# 1 MiB WAL files keep the per-minute archive small; gzip shrinks idle files further.
export POSTGRES_INITDB_ARGS="--wal-segsize=1"
latest=$(ls -1d /data/pg/base/2*Z/ 2>/dev/null | sort | tail -1 || true)
if [ ! -s "$PGDATA/PG_VERSION" ] && [ -n "$latest" ] && [ -f "$latest/base.tar.gz" ]; then
  # The local disk was reset (Pod restart): rebuild from the newest base backup, then replay the archive.
  echo "Restoring PostgreSQL from $latest and the WAL archive"
  gosu postgres tar -xzf "$latest/base.tar.gz" -C "$PGDATA"
  gosu postgres touch "$PGDATA/recovery.signal"
elif [ ! -s "$PGDATA/PG_VERSION" ]; then
  if [ -n "$(ls -A /data/pg/wal)" ]; then
    # WAL without any base backup cannot be replayed; keep it aside so new WAL names never collide.
    mv /data/pg/wal "/data/pg/wal.orphan-$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir /data/pg/wal && chown postgres:postgres /data/pg/wal
  fi
  # Reuse official image initialization, then immediately stop its server before supervisor takes ownership.
  /usr/local/bin/docker-entrypoint.sh postgres -c listen_addresses=127.0.0.1 &
  bootstrap_pid=$!
  trap 'kill -TERM "$bootstrap_pid" 2>/dev/null || true' EXIT
  for i in $(seq 1 120); do
    if gosu postgres psql -h /var/run/postgresql -U postgres -d audigo_video -c 'SELECT 1' >/dev/null 2>&1; then break; fi
    kill -0 "$bootstrap_pid"
    sleep 1
  done
  # Wait for final server (entrypoint temporarily starts another server during init).
  until [ "$(head -1 "$PGDATA/postmaster.pid" 2>/dev/null || true)" = "$bootstrap_pid" ]; do
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
