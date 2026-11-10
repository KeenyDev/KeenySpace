#!/usr/bin/env bash
#
# Unattended backup for a single-host compose deployment. Run by
# keenyspace-backup.service; reads its settings from /etc/keenyspace/backup.env
# (see deploy/host/backup.env.example).
#
#   1. Recreates the keenyspace container with KEENYSPACE_ADMIN_API_ENABLED=1.
#   2. Streams POST /v1/admin/backup (canon markdown + KeenySpace Postgres in one
#      consistent tarball) using an admin-group API key.
#   3. Dumps the Authentik database, which the KeenySpace backup does not cover.
#   4. Recreates the container without the flag again, on every exit path, so
#      the admin API is mounted only for the duration of the run.
#   5. Ships the files off-host with rclone and prunes old copies.
#
# Each container recreate is a short outage (seconds) and interrupts an
# in-flight compile; the compile backstop picks the work up again.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKUP_ENV="${BACKUP_ENV:-/etc/keenyspace/backup.env}"

if [ -f "$BACKUP_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$BACKUP_ENV"
  set +a
fi

: "${KEENYSPACE_BACKUP_API_KEY:?set KEENYSPACE_BACKUP_API_KEY in $BACKUP_ENV}"
: "${BACKUP_REMOTE:?set BACKUP_REMOTE (rclone remote:path) in $BACKUP_ENV}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/keenyspace}"
LOCAL_RETENTION_DAYS="${LOCAL_RETENTION_DAYS:-3}"
REMOTE_RETENTION_DAYS="${REMOTE_RETENTION_DAYS:-30}"
KS_BASE_URL="${KS_BASE_URL:-http://127.0.0.1:8000}"
COMPOSE=(docker compose -f "$REPO_DIR/deploy/docker-compose.yml" -f "$REPO_DIR/deploy/docker-compose.prod.yml")

exec 9> /run/keenyspace-backup.lock
if ! flock -n 9; then
  echo "another backup run holds /run/keenyspace-backup.lock" >&2
  exit 1
fi

wait_healthy() {
  local tries=0
  until curl -sf "$KS_BASE_URL/healthz" > /dev/null; do
    tries=$((tries + 1))
    if [ "$tries" -gt 90 ]; then
      echo "keenyspace did not become healthy at $KS_BASE_URL/healthz" >&2
      return 1
    fi
    sleep 2
  done
}

disable_admin_api() {
  KEENYSPACE_ADMIN_API_ENABLED=0 "${COMPOSE[@]}" up -d --no-deps keenyspace
  wait_healthy
}
trap disable_admin_api EXIT

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$BACKUP_DIR"
ks_archive="$BACKUP_DIR/keenyspace-$stamp.tar.gz"
authentik_dump="$BACKUP_DIR/authentik-$stamp.sql.gz"

KEENYSPACE_ADMIN_API_ENABLED=1 "${COMPOSE[@]}" up -d --no-deps keenyspace
wait_healthy

curl -fsS -X POST \
  -H @- \
  -o "$ks_archive.partial" \
  "$KS_BASE_URL/v1/admin/backup" <<< "Authorization: Bearer $KEENYSPACE_BACKUP_API_KEY"
gzip -t "$ks_archive.partial"
mv "$ks_archive.partial" "$ks_archive"

"${COMPOSE[@]}" exec -T authentik-postgresql \
  pg_dump -U authentik -d authentik --no-owner --no-acl \
  | gzip > "$authentik_dump.partial"
gzip -t "$authentik_dump.partial"
mv "$authentik_dump.partial" "$authentik_dump"

trap - EXIT
disable_admin_api

rclone copy --no-traverse "$ks_archive" "$BACKUP_REMOTE"
rclone copy --no-traverse "$authentik_dump" "$BACKUP_REMOTE"
rclone delete --min-age "${REMOTE_RETENTION_DAYS}d" "$BACKUP_REMOTE"

find "$BACKUP_DIR" -maxdepth 1 -type f -mtime "+$LOCAL_RETENTION_DAYS" -delete

echo "backup complete: $(basename "$ks_archive") $(basename "$authentik_dump") -> $BACKUP_REMOTE"
