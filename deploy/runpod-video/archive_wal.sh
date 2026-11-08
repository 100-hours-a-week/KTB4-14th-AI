#!/bin/bash
# PostgreSQL archive_command: gzip one WAL file onto the network volume atomically.
# PGDATA lives on the Pod's local disk because RunPod network volumes (MooseFS) report 0777 to
# non-root users and PostgreSQL refuses such a data directory; this archive is what makes it durable.
set -euo pipefail
src=$1
target=/data/pg/wal/$2.gz
if [ -f "$target" ]; then
  # A retry after a crash may find an identical copy; anything else must stop archiving loudly.
  gunzip -c "$target" | cmp -s - "$src" && exit 0
  echo "archive_wal: $target exists with different content" >&2
  exit 1
fi
gzip -c "$src" > "$target.tmp.$$"
mv "$target.tmp.$$" "$target"
