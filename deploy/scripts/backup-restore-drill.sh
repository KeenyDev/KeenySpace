#!/usr/bin/env bash
#
# ============================================================================
# WARNING - DESTRUCTIVE: this drill runs `docker compose down -v`, which
# DESTROYS the compose project's named volumes, including keenyspace-fs
# (all workspaces) and the Postgres data. NEVER run this against a live
# dogfood/production project. Run it under a throwaway project name:
#
#   COMPOSE_PROJECT_NAME=ks-drill bash deploy/scripts/backup-restore-drill.sh
#
# In CI this runs on a throwaway runner, so the wipe is harmless there.
# ============================================================================
#
# REL-07 backup-restore drill: backup -> down -v -> up -> restore -> assert
# workspaces survived. The tarball is downloaded to the runner filesystem
# BEFORE down -v (named volumes do not survive it).
#
# Prerequisites:
#   - `keenyspace` CLI installed and authenticated (auth.json populated)
#   - at least one workspace exists before the drill (seed one if empty)
#   - jq + curl on PATH
#
# Note: `down -v` also wipes server-side auth state (api keys live in
# Postgres). If the CLI token will not survive the wipe, set DRILL_REAUTH_CMD
# to a command that re-establishes CLI auth; it runs after the second `up`
# and before `restore`.

set -euo pipefail

COMPOSE="docker compose ${DRILL_COMPOSE_FILES:--f deploy/docker-compose.yml}"
KS_BASE_URL="${KS_BASE_URL:-http://localhost:8000}"
BACKUP_PATH="${BACKUP_PATH:-/tmp/drill-backup.tar.gz}"
DRILL_REAUTH_CMD="${DRILL_REAUTH_CMD:-}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/keenyspace"

export KEENYSPACE_ADMIN_API_ENABLED=1

wait_healthy() {
  local tries=0
  until curl -sf "$KS_BASE_URL/healthz" > /dev/null; do
    tries=$((tries + 1))
    if [ "$tries" -gt 60 ]; then
      echo "FAIL: server did not become healthy at $KS_BASE_URL/healthz" >&2
      exit 1
    fi
    sleep 2
  done
}

workspace_count() {
  local token
  token=$(jq -r '.api_key // .access_token // empty' "$CONFIG_DIR/auth.json")
  if [ -z "$token" ]; then
    echo "FAIL: no CLI auth token in $CONFIG_DIR/auth.json" >&2
    exit 1
  fi
  curl -sf -H "Authorization: Bearer $token" \
    "$KS_BASE_URL/v1/api/workspaces/?status=active" | jq '.workspaces | length'
}

$COMPOSE up -d
wait_healthy

keenyspace backup --output "$BACKUP_PATH"
test -s "$BACKUP_PATH" || { echo "FAIL: backup tarball missing or empty at $BACKUP_PATH" >&2; exit 1; }
echo "Backup tarball on runner FS: $BACKUP_PATH ($(wc -c < "$BACKUP_PATH") bytes)"

$COMPOSE down -v

$COMPOSE up -d
wait_healthy

if [ -n "$DRILL_REAUTH_CMD" ]; then
  bash -c "$DRILL_REAUTH_CMD"
fi

keenyspace restore "$BACKUP_PATH"

WORKSPACES=$(workspace_count)
if [ "$WORKSPACES" -gt 0 ]; then
  echo "Backup-restore drill PASSED - $WORKSPACES workspace(s) intact"
else
  echo "FAIL: no workspaces after restore" >&2
  exit 1
fi
